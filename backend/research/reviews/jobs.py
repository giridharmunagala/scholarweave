from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any, overload
from urllib.parse import urlsplit, urlunsplit

from openai import NotFoundError as ProviderNotFoundError

from backend.agents.blueprint import AgentBlueprint, ModelReferenceSpec
from backend.agents.compiler import AgentCompiler, CompiledAgent
from backend.agents.context_budget import ContextBudgetPolicy, model_context_budget
from backend.agents.input_budget import InputBudget
from backend.core.errors import NotFoundError, ValidationError
from backend.prompting.registry import PromptRegistry
from backend.providers.decisions import decision_input_size, score_decisions
from backend.providers.inference import inference_priority
from backend.providers.runtime import ModelRuntime
from backend.providers.reasoning import ReasoningEffort
from backend.providers.types import ModelReference
from backend.research.reviews.repository import ReviewRepository
from backend.research.reviews.schemas import DecisionRole, ReviewSchema
from backend.runs.service import RunService


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


class ReviewPaused(Exception):
    pass


class InvalidReviewOutput(ValidationError):
    """Only a settled worker's schema or evidence validation failed."""


class ReviewJobs:
    def __init__(
        self, repository: ReviewRepository, runs: RunService, compiler: AgentCompiler,
        runtime: ModelRuntime, prompts: PromptRegistry,
    ) -> None:
        self.repository = repository
        self.runs = runs
        self.compiler = compiler
        self.runtime = runtime
        self.prompts = prompts

    def usage(self, review_id: str) -> tuple[int, bool]:
        jobs = self.repository.jobs(review_id)
        return (
            sum(job.get("tokens_spent", 0) for job in jobs),
            any(job.get("usage_estimated", False) for job in jobs),
        )

    def checkpoint(self, review_id: str, **updates: Any) -> None:
        record = self.repository.get(review_id)
        self.repository.update(review_id, {**record.state, **updates}, record.revision)

    def reserve(self, review_id: str, tokens: int) -> None:
        record = self.repository.get(review_id)
        if record.state["status"] != "running":
            raise ReviewPaused()
        allowance = record.state.get("allowance_tokens") if not record.state.get("work_budget") else None
        used, _ = self.usage(review_id)
        if allowance is not None and used + tokens > allowance:
            self.checkpoint(
                review_id, status="awaiting_approval", approval="budget",
                suggested_allowance_tokens=used + tokens,
                error="The next isolated task needs more than the remaining overall allowance. Extend it, pause, or stop this stage.",
            )
            raise ReviewPaused()

    def work_usage(self, review_id: str) -> dict[str, int]:
        return {kind: sum(job.get("work_kind") == kind for job in self.repository.jobs(review_id, "work_budget"))
                for kind in ("screened_papers", "read_papers", "comparison_passes")}

    def admit_work(self, review_id: str, kind: str, identifier: str) -> None:
        key = f"work:{kind}:{digest(identifier)}"
        if self.repository.job(review_id, key):
            return
        state = self.repository.get(review_id).state
        if state["status"] != "running":
            raise ReviewPaused()
        budget = state.get("work_budget")
        if budget and self.work_usage(review_id)[kind] >= budget[kind]:
            self.checkpoint(
                review_id, status="awaiting_approval", approval="budget",
                suggested_work_budget={**budget, kind: budget[kind] + 1},
                error=f"The {kind.replace('_', ' ')} limit is exhausted. Extend this work budget or pause.",
            )
            raise ReviewPaused()
        self.repository.save_job(review_id, key, "work_budget", {"key": key, "work_kind": kind})

    def prepare(
        self, reference: ModelReferenceSpec, prompt_id: str, schema: type[ReviewSchema] | None = None, *, input_tokens: int | None = None,
        max_output_tokens: int | None = None, reasoning_effort: ReasoningEffort | None = None,
        context_window_tokens: int | None = None, output_reserve_fraction: float | None = None,
    ) -> tuple[CompiledAgent, InputBudget, int]:
        resolved = self.runtime.resolve("chat", model_reference=ModelReference.model_validate(reference.model_dump()))
        window = resolved.context_window_tokens or self.runtime.settings.agent_context_window_tokens
        if context_window_tokens is not None:
            window = min(window, context_window_tokens)
        reserved_output = int(window * output_reserve_fraction) if output_reserve_fraction is not None else max_output_tokens
        input_limit, output_limit, _ = model_context_budget(window, reserved_output)
        if max_output_tokens is not None:
            output_limit = min(output_limit, max_output_tokens)
        blueprint = AgentBlueprint.model_validate({
            "name": f"Literature review: {prompt_id}",
            "entry_agent_id": "review_worker",
            "run": {"max_turns": 1},
            "agents": [{
                "id": "review_worker", "name": "Isolated literature worker",
                "instructions": self.prompts.render(prompt_id),
                "model": reference.model_dump(),
                "model_settings": {
                    "max_tokens": output_limit,
                    **({"reasoning": {"effort": reasoning_effort}} if reasoning_effort is not None else {}),
                },
                **({"output": {"kind": "json_schema", "name": schema.__name__, "schema": schema.model_json_schema()}}
                   if schema is not None else {}),
            }],
        })
        compiled = self.compiler.compile(blueprint, context_window_tokens=window)
        compiled = replace(compiled, context_policy=ContextBudgetPolicy(
            self.runtime.settings, context_window_tokens_by_agent={"review_worker": window},
            prompt_registry=self.prompts,
        ))
        budget = InputBudget(resolved.model, min(input_limit, input_tokens) if input_tokens is not None else input_limit)
        # The wire schema and runtime system information are additional to agent instructions.
        budget.limit -= (budget.count("", json_text(schema.model_json_schema())) if schema is not None else 0) + 1024
        if budget.limit <= 0:
            raise ValidationError("The selected model context cannot accommodate this worker's instructions and request overhead.")
        if prompt_id == "literature-analysis-sources":
            async def count_request(parameters: dict) -> int | None:
                if budget.encoding is not None:
                    return 32 + len(budget.encoding.encode(json_text(parameters), disallowed_special=()))
                if budget.method != "provider-tokenize":
                    return None
                return await self._provider_input_count(
                    json_text(parameters), (compiled, budget, output_limit), serialized_request=True,
                )

            compiled = replace(compiled, context_policy=ContextBudgetPolicy(
                self.runtime.settings, context_window_tokens_by_agent={"review_worker": window},
                prompt_registry=self.prompts, request_token_counter=count_request,
            ))
        return compiled, budget, output_limit

    def packets(
        self, records: Iterable[dict], frame: dict, prepared: tuple[CompiledAgent, InputBudget, int],
    ) -> list[dict]:
        compiled, budget, _ = prepared
        packets: list[dict] = []
        current: list[dict] = []
        for record in records:
            candidate = {**frame, "records": [*current, record]}
            if budget.count(compiled.entry_agent.instructions, json_text(candidate)) <= budget.limit:
                current.append(record)
                continue
            if current:
                packets.append({**frame, "records": current})
            single = {**frame, "records": [record]}
            if budget.count(compiled.entry_agent.instructions, json_text(single)) > budget.limit:
                raise ValidationError("One complete review record exceeds this model's input budget. Use a larger context or a narrower objective.")
            current = [record]
        if current:
            packets.append({**frame, "records": current})
        return packets

    async def _provider_input_count(
        self, prompt: str, prepared: tuple[CompiledAgent, InputBudget, int],
        *, serialized_request: bool = False,
    ) -> int:
        compiled, budget, _ = prepared
        system = "" if serialized_request else compiled.entry_agent.instructions
        cached = budget.cached_count(system, prompt)
        if cached is not None:
            return cached
        binding = compiled.entry_agent.binding
        url = urlsplit(str(binding.client.base_url))
        path = url.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        endpoint = urlunsplit((url.scheme, url.netloc, f"{path}/tokenize", "", ""))
        with inference_priority("background"):
            async with self.runtime.inference_scheduler.request(
                profile_id=binding.provider_profile_id, model=binding.model_name,
                server_url=str(binding.client.base_url).rstrip("/"),
            ):
                result = await binding.client.post(endpoint, cast_to=object, body={
                    "model": binding.model_name,
                    "content": prompt if serialized_request else budget.serialize(system, prompt),
                })
        if not isinstance(result, dict) or not isinstance(result.get("tokens"), list) or not result["tokens"] or not all(
            isinstance(token, int) and not isinstance(token, bool) for token in result["tokens"]
        ):
            raise ValidationError("The inference server returned an invalid tokenizer response; no oversized summary request was sent.")
        count = len(result["tokens"]) + 32
        budget.remember_count(system, prompt, count)
        return count

    async def summary_packets(
        self, review_id: str, records: list[dict], frame: dict,
        prepared: tuple[CompiledAgent, InputBudget, int],
    ) -> list[dict]:
        compiled, budget, _ = prepared
        if budget.encoding is not None:
            return self.packets(records, frame, prepared)
        if compiled.entry_agent.binding.provider_kind != "openai_compatible":
            self._tokenizer_notice(review_id)
            return self.packets(records, frame, prepared)
        try:
            await self._provider_input_count(json_text({**frame, "records": []}), prepared)
        except ProviderNotFoundError:
            self._tokenizer_notice(review_id)
            return self.packets(records, frame, prepared)
        budget.method = "provider-tokenize"
        packets = []
        offset = 0
        while offset < len(records):
            if self.repository.get(review_id).state["status"] != "running":
                raise ReviewPaused()
            low, high = 1, len(records) - offset
            fitting = 0
            while low <= high:
                size = (low + high) // 2
                packet = {**frame, "records": records[offset:offset + size]}
                count = await self._provider_input_count(json_text(packet), prepared)
                if count <= budget.limit:
                    fitting, low = size, size + 1
                else:
                    high = size - 1
            if not fitting:
                raise ValidationError("One complete summary exceeds the model's measured input budget. Use a larger context or a narrower objective.")
            packets.append({**frame, "records": records[offset:offset + fitting]})
            offset += fitting
        return packets

    def _tokenizer_notice(self, review_id: str) -> None:
        state = self.repository.get(review_id).state
        notice = "Exact tokenization is unavailable for summary selection; conservative byte-based batches may underfill the model context."
        if notice not in state.get("notices", []):
            self.checkpoint(review_id, notices=[*state.get("notices", []), notice])

    @overload
    async def call(
        self, review_id: str, stage: str, key: str, packet: dict,
        prepared: tuple[CompiledAgent, InputBudget, int], schema: type[ReviewSchema],
        validate: Callable[[dict], None] | None = None,
    ) -> dict: ...

    @overload
    async def call(
        self, review_id: str, stage: str, key: str, packet: dict,
        prepared: tuple[CompiledAgent, InputBudget, int], schema: None = None,
        validate: None = None,
    ) -> str: ...

    async def call(
        self, review_id: str, stage: str, key: str, packet: dict,
        prepared: tuple[CompiledAgent, InputBudget, int], schema: type[ReviewSchema] | None = None,
        validate: Callable[[dict], None] | None = None,
    ) -> dict | str:
        if schema is None:
            return await self._call(review_id, stage, key, packet, prepared, None)
        current = packet
        for attempt in range(3):
            if attempt and self.repository.get(review_id).state["status"] != "running":
                raise ReviewPaused()
            try:
                return await self._call(
                    review_id, stage, key if attempt == 0 else f"{key}:repair:{attempt}",
                    current, prepared, schema, validate,
                )
            except InvalidReviewOutput as exc:
                if attempt == 2:
                    raise
                record = self.repository.get(review_id)
                notice = f"{key}: invalid worker output; automatically repairing ({attempt + 1}/2)."
                notices = record.state.get("notices", [])
                if notice not in notices:
                    self.checkpoint(review_id, notices=[*notices, notice])
                current = {
                    **packet, "validation_feedback": str(exc)[:4000],
                    "repair_instruction": "Return a corrected complete result using only the original admitted records. "
                    "Resolve the validation error; do not invent evidence or omit required records.",
                }
                compiled, budget, _ = prepared
                size = (await self._provider_input_count(json_text(current), prepared)
                        if budget.method == "provider-tokenize"
                        else budget.count(compiled.entry_agent.instructions, json_text(current)))
                if size > budget.limit:
                    raise InvalidReviewOutput(
                        f"{exc}; corrective feedback cannot fit the admitted context. No truncated repair was sent."
                    ) from exc
        raise AssertionError("Unreachable repair loop")

    async def _call(
        self, review_id: str, stage: str, key: str, packet: dict,
        prepared: tuple[CompiledAgent, InputBudget, int], schema: type[ReviewSchema] | None,
        validate: Callable[[dict], None] | None = None,
    ) -> dict | str:
        compiled, budget, reserve = prepared
        prompt = json_text(packet)
        binding = compiled.entry_agent.binding
        provenance = {
            "model": binding.model_name, "provider_profile_id": binding.provider_profile_id,
            "api_mode": binding.api_mode, "context_tokens": compiled.context_window_tokens,
            "prompt_hash": digest(compiled.entry_agent.instructions), "input_hash": digest(packet),
            "endpoint_hash": digest(str(binding.client.base_url)), "provider_kind": binding.provider_kind,
        }
        job_key = f"{key}:{digest(provenance)}"
        state = self.repository.get(review_id).state
        if state.get("main_model") == {
            "provider_profile_id": binding.provider_profile_id, "model": binding.model_name,
        }:
            for previous in self.repository.jobs(review_id, stage, key_prefix=f"{key}:"):
                prior_provenance = previous.get("provenance", {})
                if (
                    previous.get("status") in {"completed", "settled"}
                    and previous.get("key") == f"{key}:{digest(prior_provenance)}"
                    and all(prior_provenance.get(field) == provenance[field] for field in ("prompt_hash", "input_hash"))
                ):
                    return self._validate_result(review_id, previous["key"], stage, {
                        **previous, "raw_result": previous.get("result", previous.get("raw_result")),
                    }, schema, validate)
        saved = self.repository.job(review_id, job_key) or {
            "key": job_key, "stage": stage, "provenance": provenance, "tokens_spent": 0,
        }
        if saved.get("status") == "completed":
            return self._validate_result(review_id, job_key, stage, {
                **saved, "raw_result": saved["result"],
            }, schema, validate)
        if saved.get("status") == "invalid":
            error = saved["validation_error"]
            if not saved.get("structured_output_error"):
                error = f"An isolated worker returned invalid evidence: {error}"
            raise InvalidReviewOutput(error)
        if saved.get("status") == "settled":
            return self._validate_result(review_id, job_key, stage, saved, schema, validate)
        estimated = (await self._provider_input_count(prompt, prepared)
                     if budget.method == "provider-tokenize" else budget.count(compiled.entry_agent.instructions, prompt))
        if estimated > budget.limit:
            raise ValidationError("Review input exceeds its admitted context. No truncated request was sent.")
        if self.repository.get(review_id).state["status"] != "running":
            raise ReviewPaused()
        run = None
        if saved.get("run_id"):
            try:
                run = self.runs.get(saved["run_id"])
            except NotFoundError:
                if saved.get("status") != "starting":
                    raise ValidationError("An unfinished review task's run is missing. Its outcome cannot be assumed.")
        if run is None or run.status in {"failed", "cancelled"}:
            if saved.get("status") != "starting":
                overhead = (budget.count("", json_text(schema.model_json_schema())) if schema is not None else 0) + 1024
                self.reserve(review_id, estimated + overhead + reserve)
                saved = {
                    **saved, "run_id": str(uuid.uuid4()), "status": "starting",
                    "reservation": estimated + overhead + reserve,
                    "previous_tokens": saved.get("tokens_spent", 0),
                    "previous_estimated": saved.get("usage_estimated", False),
                    "tokens_spent": saved.get("tokens_spent", 0) + estimated + overhead + reserve,
                    "usage_estimated": True,
                }
                self.repository.save_job(review_id, job_key, stage, saved)
            with inference_priority("background"):
                run = self.runs.create(
                    compiled, prompt, conversation_id=None,
                    runtime_metadata={"autonomous_work": True, "literature_review_id": review_id},
                    run_id=saved["run_id"],
                )
            saved["status"] = "running"
            try:
                self.repository.save_job(review_id, job_key, stage, saved)
            except Exception:
                await self.runs.cancel(run.id)
                raise
        self.checkpoint(review_id, active_run_id=run.id)
        try:
            while run.status in {"pending", "running"}:
                await asyncio.sleep(0.05)
                run = self.runs.get(run.id)
        except asyncio.CancelledError:
            await self.runs.cancel(run.id)
            run = self.runs.get(run.id)
            self._account(review_id, job_key, stage, saved, run)
            raise
        self._account(review_id, job_key, stage, saved, run)
        self.checkpoint(review_id, active_run_id=self.repository.running_run(review_id))
        if run.status != "completed":
            error = run.error or ""
            if schema is not None and run.status == "failed" and (
                f"Model returned invalid JSON for structured output '{schema.__name__}'" in error
                or f"Structured output '{schema.__name__}' failed JSON Schema validation:" in error
            ):
                self.repository.save_job(review_id, job_key, stage, {
                    **saved, "status": "invalid", "run_id": None, "validation_error": error,
                    "structured_output_error": True,
                })
                raise InvalidReviewOutput(error)
            raise ValidationError(run.error or f"The isolated review task was {run.status}. Resume retries only this unfinished task.")
        return self._validate_result(review_id, job_key, stage, saved, schema, validate)

    def _validate_result(
        self, review_id: str, key: str, stage: str, saved: dict, schema: type[ReviewSchema] | None,
        validate: Callable[[dict], None] | None,
    ) -> dict | str:
        if schema is None:
            result = saved["raw_result"]
            self.repository.save_job(review_id, key, stage, {**saved, "status": "completed", "result": result})
            return result
        try:
            result = schema.model_validate(saved["raw_result"]).model_dump(mode="json")
            if validate:
                validate(result)
        except (ValueError, ValidationError) as exc:
            self.repository.save_job(review_id, key, stage, {
                **saved, "status": "invalid", "run_id": None, "validation_error": str(exc),
            })
            raise InvalidReviewOutput(f"An isolated worker returned invalid evidence: {exc}") from exc
        self.repository.save_job(review_id, key, stage, {**saved, "status": "completed", "result": result})
        return result

    async def recover(self, review_id: str) -> None:
        record = self.repository.get(review_id)
        for job in self.repository.jobs(review_id):
            if job.get("status") not in {"starting", "running"} or not job.get("run_id"):
                continue
            try:
                run = self.runs.get(job["run_id"])
            except NotFoundError:
                continue
            if record.state["status"] != "running" and run.status in {"pending", "running"}:
                await self.runs.cancel(run.id)
                run = self.runs.get(run.id)
            if run.status not in {"pending", "running"}:
                self._account(review_id, job["key"], job["stage"], job, run)

    def _account(self, review_id: str, key: str, stage: str, saved: dict, run: Any) -> None:
        usage = run.usage_json or {}
        tokens = usage.get("total_tokens") or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
        estimated = not bool(tokens)
        if estimated:
            tokens = saved.get("reservation", 0)
        saved.update({
            "tokens_spent": saved.get("previous_tokens", 0) + int(tokens),
            "usage_estimated": saved.get("previous_estimated", False) or estimated,
            "status": "settled" if run.status == "completed" else run.status,
            "raw_result": run.final_output_json,
        })
        self.repository.save_job(review_id, key, stage, saved)

    async def native(
        self, review_id: str, stage: str, key: str, role: DecisionRole, state: dict, questions: dict,
    ) -> dict:
        resolved = self.runtime.resolve("decision", model_reference=ModelReference.model_validate(role.model_reference.model_dump()))
        provenance = {
            "role": role.model_dump(mode="json"), "state_hash": digest(state), "rubric_hash": digest(questions),
            "endpoint_hash": digest(resolved.base_url), "provider_kind": resolved.kind,
            "context_tokens": resolved.context_window_tokens,
        }
        job_key = f"{key}:{digest(provenance)}"
        saved = self.repository.job(review_id, job_key)
        if saved and saved.get("status") == "completed":
            return saved["result"]
        size = decision_input_size(state, questions)
        if size > min(role.input_tokens, resolved.context_window_tokens or role.input_tokens):
            raise ValidationError("The decision model cannot admit this complete metadata record within its input budget.")
        estimate = size * len(questions)
        self.reserve(review_id, estimate)
        previous_estimated = (saved or {}).get("usage_estimated", False)
        saved = {
            "key": job_key, "stage": stage, "status": "running", "provenance": provenance,
            "tokens_spent": (saved or {}).get("tokens_spent", 0) + estimate, "usage_estimated": True,
        }
        self.repository.save_job(review_id, job_key, stage, saved)
        try:
            with inference_priority("background"):
                result = await score_decisions(
                    self.runtime, ModelReference.model_validate(role.model_reference.model_dump()),
                    state, questions, input_tokens=role.input_tokens,
                )
        except BaseException:
            self.repository.save_job(review_id, job_key, stage, {**saved, "status": "interrupted"})
            raise
        usage = result.get("usage") or {}
        actual = usage.get("input_tokens")
        self.repository.save_job(review_id, job_key, stage, {
            **saved, "status": "completed", "result": result,
            "tokens_spent": saved["tokens_spent"] - estimate + (actual if actual is not None else estimate),
            "usage_estimated": previous_estimated or actual is None,
        })
        return result
