from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.agents.blueprint import AgentBlueprint
from backend.app import create_app
from backend.conversations.turns import deep_work_blueprint, research_blueprint
from backend.core.errors import ValidationError
from backend.persistence.files import SafeStorage
from backend.prompting.registry import PromptRegistry
from backend.prompting.skills import (
    MAX_SKILL_BYTES,
    MAX_SKILLS,
    MAX_TOTAL_SKILL_BYTES,
    SkillRegistry,
)
from backend.research.sources import WebSource, WebSourceUnavailable
from backend.tests.test_api import configure_provider, wait_for_run
from backend.utils import utcnow


def _write_skill(settings, name: str, content: str | bytes) -> Path:
    data = content.encode("utf-8") if isinstance(content, str) else content
    return SafeStorage(settings).write_bytes(
        settings.data_dir, f"skills/{name}", data,
    ).absolute_path


def _registry(settings, *, empty_defaults: bool = False) -> SkillRegistry:
    defaults = (
        settings.data_dir / "empty-defaults"
        if empty_defaults else Path(__file__).parents[1] / "prompting" / "defaults" / "skills"
    )
    return SkillRegistry(defaults, SafeStorage(settings))


def test_local_skills_override_defaults_and_reload_in_stable_order(test_settings):
    registry = _registry(test_settings)
    assert [skill.name for skill in registry.snapshot()] == ["web-synthesis"]
    _write_skill(test_settings, "web-synthesis.md", "# My web recipe\n\nCompare the sources.")
    first_path = _write_skill(test_settings, "a-recipe.md", b"\xef\xbb\xbf# A recipe\n\nUse local notes.")
    _write_skill(test_settings, "ignored.py", "raise RuntimeError('never execute')")
    _write_skill(test_settings, "nested/not-loaded.md", "Not a top-level skill.")

    first = registry.snapshot()
    assert [skill.name for skill in first] == ["a-recipe", "web-synthesis"]
    assert first[0].instructions.startswith("# A recipe")
    assert first[1].instructions == "# My web recipe\n\nCompare the sources."
    _write_skill(test_settings, "web-synthesis.md", "# Changed\n\nReturn a comparison table.")
    first_path.unlink()
    second = registry.snapshot()
    assert [skill.name for skill in second] == ["web-synthesis"]
    assert second[0].instructions.startswith("# Changed")
    assert first[1].instructions.startswith("# My web recipe")


@pytest.mark.parametrize(
    ("name", "content", "error"),
    [
        ("empty.md", " \n\t", "non-empty"),
        ("bad-utf8.md", b"\xff", "Cannot read skill instructions"),
        ("Bad_Name.md", "# Invalid name", "Invalid skill filename"),
        ("bad--name.md", "# Invalid name", "Invalid skill filename"),
        ("x" * 65 + ".md", "# Too long", "Invalid skill filename"),
        ("upper.MD", "# Wrong extension", "Invalid skill filename"),
        ("large.md", b"x" * (MAX_SKILL_BYTES + 1), "exceeds"),
    ],
    ids=["empty", "utf8", "name", "hyphens", "long-name", "extension", "size"],
)
def test_invalid_skills_fail_explicitly(test_settings, name, content, error):
    _write_skill(test_settings, name, content)
    with pytest.raises(ValidationError, match=error):
        _registry(test_settings).snapshot()


def test_skill_size_limits_use_utf8_bytes_and_accept_exact_boundary(test_settings):
    registry = _registry(test_settings, empty_defaults=True)
    content = "\u00e9" * (MAX_SKILL_BYTES // 2)
    _write_skill(test_settings, "boundary.md", content)
    assert registry.snapshot()[0].size_bytes == MAX_SKILL_BYTES
    _write_skill(test_settings, "boundary.md", content + "x")
    with pytest.raises(ValidationError, match="exceeds"):
        registry.snapshot()


def test_total_skill_budget_and_count_are_enforced(test_settings):
    registry = _registry(test_settings, empty_defaults=True)
    for index in range(MAX_TOTAL_SKILL_BYTES // MAX_SKILL_BYTES):
        _write_skill(test_settings, f"large-{index}.md", "x" * MAX_SKILL_BYTES)
    assert sum(skill.size_bytes for skill in registry.snapshot()) == MAX_TOTAL_SKILL_BYTES
    _write_skill(test_settings, "extra.md", "x")
    with pytest.raises(ValidationError, match="Combined skill instructions"):
        registry.snapshot()
    for index in range(MAX_SKILLS + 1):
        _write_skill(test_settings, f"small-{index}.md", "x")
    with pytest.raises(ValidationError, match="Markdown files"):
        registry.snapshot()


def test_local_and_packaged_skills_share_one_budget(test_settings):
    for index in range(MAX_SKILLS):
        _write_skill(test_settings, f"local-{index}.md", "Use existing tools.")
    with pytest.raises(ValidationError, match="At most"):
        _registry(test_settings).snapshot()


def test_skill_directory_must_be_readable_directory(test_settings):
    SafeStorage(test_settings).write_bytes(test_settings.data_dir, "skills", b"not a directory")
    with pytest.raises(ValidationError, match="Cannot read skill instructions"):
        _registry(test_settings).snapshot()


def test_skills_cannot_follow_links_outside_their_directory(test_settings):
    target = SafeStorage(test_settings).write_bytes(
        test_settings.data_dir, "outside.md", b"Not installed as a skill.",
    ).absolute_path
    installed = _write_skill(test_settings, "linked.md", "Temporary.")
    installed.unlink()
    try:
        installed.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this system.")
    with pytest.raises(ValidationError, match="escapes"):
        _registry(test_settings).snapshot()


@pytest.mark.parametrize("effort", ["auto", "quick", "thorough"])
def test_skills_reuse_tools_without_expanding_capabilities(test_settings, effort):
    _write_skill(test_settings, "local-recipe.md", """# Local recipe

Use for note comparisons. Use create_work_plan for substantial tasks.

```json
{"tools": [{"name": "run_shell"}]}
```
""")
    registry = PromptRegistry(storage=SafeStorage(test_settings))
    blueprint = (
        deep_work_blueprint({}, prompts=registry, web_enabled=False)
        if effort == "thorough"
        else research_blueprint({}, prompts=registry, web_enabled=False, response_effort=effort)
    )
    baseline = (
        deep_work_blueprint({}, web_enabled=False)
        if effort == "thorough"
        else research_blueprint({}, web_enabled=False, response_effort=effort)
    )
    assert blueprint.tools == baseline.tools
    assert blueprint.agent_tools == baseline.agent_tools
    for agent in blueprint.agents:
        assert '<skill name="local-recipe">' in agent.instructions
        assert '<skill name="web-synthesis">' in agent.instructions
        assert "A skill alone never authorizes a write" in agent.instructions
        assert "actually available to this agent" in agent.instructions
        assert "acquire-source" not in agent.tool_ids
        assert "run_shell" not in agent.tool_ids
    if effort == "thorough":
        assert "create-work-plan" not in blueprint.agents[1].tool_ids


def test_legacy_fast_answer_does_not_load_skills(test_settings):
    _write_skill(test_settings, "broken.md", "")
    registry = PromptRegistry(storage=SafeStorage(test_settings))
    blueprint = research_blueprint({}, prompts=registry, fast_answer=True)
    assert "<skill " not in blueprint.agents[0].instructions
    assert {tool.id for tool in blueprint.tools} == {
        "search-sources", "acquire-source", "read-web-page",
    }


def test_custom_skills_reload_per_message_and_persist_in_run_snapshots(
    test_settings, stub_provider,
):
    _write_skill(test_settings, "local-style.md", "# Local style\n\nFirst recipe version.")
    with TestClient(create_app(test_settings)) as client:
        configure_provider(client, stub_provider)
        services = client.app.state.services
        conversation = client.post("/api/agent/conversations", json={}).json()
        records = []
        for index, version in enumerate(["First", "Second"], start=1):
            _write_skill(test_settings, "local-style.md", f"# Local style\n\n{version} recipe version.")
            response = client.post(
                f"/api/agent/conversations/{conversation['id']}/messages",
                json={"content": "Discuss my idea without saving anything.", "web_enabled": False},
            )
            assert response.status_code == 202, response.text
            run = wait_for_run(client, response.json()["run"]["id"])
            assert run["status"] == "completed", run["error"]
            record = services.runs.get(run["id"])
            records.append(record)
            assert len(stub_provider.requests) == index
            assert f"{version} recipe version." in stub_provider.requests[-1]["messages"][0]["content"]
            assert not record.tool_attempts
            assert not record.runtime_metadata_json.get("work_plan")
        assert "First recipe version." in records[0].blueprint_json["agents"][0]["instructions"]
        assert "Second recipe version." in records[1].blueprint_json["agents"][0]["instructions"]
        _write_skill(test_settings, "local-style.md", "")
        recovered = services.compiler.compile(AgentBlueprint.model_validate(records[0].blueprint_json))
        assert "First recipe version." in recovered.entry_agent.instructions
        rejected = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={"content": "A new turn.", "web_enabled": False},
        )
        assert rejected.status_code == 400, rejected.text
        assert "local-style.md" in rejected.text
        assert len(stub_provider.requests) == 2
        assert services.workspace.list_files() == []


@pytest.mark.parametrize(
    ("effort", "page_count", "save_requested"),
    [("quick", 1, True), ("auto", 2, True), ("thorough", 2, True), ("auto", 1, False)],
)
def test_web_synthesis_uses_existing_tools_and_respects_saving(
    test_settings, stub_provider, monkeypatch, effort, page_count, save_requested,
):
    test_settings.agent_epoch_max_turns = 2
    sources = [
        WebSource(
            id=f"page-{index}", url=f"https://example.com/page-{index}", title=f"Page {index}",
            chunks=(f"Verified finding {index}.",), created_at=utcnow(), expires_at=utcnow(),
        )
        for index in range(page_count)
    ]
    by_url = {source.url: source for source in sources}
    by_id = {source.id: source for source in sources}

    async def download(url):
        return by_url[url]

    async def read(source_id):
        return by_id[source_id]

    needle = "# Web synthesis"
    body = "\n\n".join(
        f"{source.chunks[0]} [{source.title}]({source.url})" for source in sources
    ) + "\n\n## Sources\n\n" + "\n".join(
        f"- [{source.title}]({source.url}); accessed {utcnow().date().isoformat()}."
        for source in sources
    )
    plans = [(needle, "search_research_notes", {
        "query": "Verified findings", "kinds": [], "tags": [], "limit": 5, "offset": 0,
    })]
    if page_count > 1:
        plans.extend([
            (needle, "create_work_plan", {"items": [
                {"id": "read", "title": "Read the supplied pages"},
                {"id": "save", "title": "Synthesize and save one sourced document"},
            ]}),
            (needle, "update_work_item", {"id": "read", "status": "in_progress", "summary": ""}),
        ])
    for source in sources:
        plans.extend([
            (needle, "acquire_research_source", {
                "kind": "web_page", "url": source.url, "title": None,
            }),
            (needle, "read_research_web_page", {
                "source_id": source.id, "query": None, "start": 0, "limit": 1,
            }),
        ])
    if page_count > 1:
        plans.append((needle, "update_work_item", {
            "id": "read", "status": "completed", "summary": "Read every supplied page.",
        }))
    if save_requested:
        plans.append((needle, "save_research_note", {
            "target": "new_note", "mode": "overwrite", "path": None, "document_id": None,
            "name": "Verified findings", "content": body, "tags": ["web-synthesis"],
            "selection": None, "expected_sha256": None,
        }))
    if page_count > 1:
        plans.append((needle, "update_work_item", {
            "id": "save", "status": "completed", "summary": "Saved one combined sourced document.",
        }))
    stub_provider.tool_plans = plans
    stub_provider.reply = body
    original_responses = stub_provider.responses

    def responses(payload):
        if not any(name == "read_research_note" for _, name, _ in stub_provider.tool_plans):
            for message in payload["messages"]:
                if message["role"] != "tool":
                    continue
                result = json.loads(message["content"])
                if isinstance(result, dict) and result.get("path", "").startswith("knowledge/"):
                    save_index = next(
                        index for index, (_, name, _) in enumerate(stub_provider.tool_plans)
                        if name == "save_research_note"
                    )
                    stub_provider.tool_plans.insert(
                        save_index + 1, (needle, "read_research_note", {"path": result["path"]}),
                    )
                    stub_provider.reply = f"Saved the sourced synthesis to {result['path']}."
                    break
        return original_responses(payload)

    monkeypatch.setattr(stub_provider, "responses", responses)
    with TestClient(create_app(test_settings)) as client:
        configure_provider(client, stub_provider)
        services = client.app.state.services
        monkeypatch.setattr(services.source_downloads, "download_web_page", download)
        monkeypatch.setattr(services.source_downloads, "get_web_source", read)
        conversation = client.post("/api/agent/conversations", json={}).json()
        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={"content": (
                "Condense these pages" + (" and save one document: " if save_requested else ": ")
                + ", ".join(by_url)
            ),
                  "response_effort": effort},
        )
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"], timeout=60)
        assert run["status"] == "completed", run["error"]
        record = services.runs.get(run["id"])
        files = services.workspace.list_files()
        assert len(record.epochs) > 1
        assert all(attempt.status == "completed" for attempt in record.tool_attempts)
        assert len(record.tool_attempts) == len(stub_provider.tool_plans)
        source_reads = [
            attempt.result_json for attempt in record.tool_attempts
            if attempt.catalog_id == "research.web.read"
        ]
        assert [result["chunks"][0]["text"] for result in source_reads] == [
            source.chunks[0] for source in sources
        ]
        if not save_requested:
            assert files == []
            assert body in run["final_output"]
            assert not any(
                attempt.catalog_id in {"research.notes.save", "research.summary.run", "work.plan.create"}
                for attempt in record.tool_attempts
            )
            return
        assert len(files) == 1
        saved = services.workspace.read_file(files[0].path)
        assert saved.content == "# Verified findings\n\n" + body + "\n"
        assert len(list(test_settings.workspace_dir.rglob("*.md"))) == 1
        assert sum(attempt.catalog_id == "research.notes.save" for attempt in record.tool_attempts) == 1
        assert not any(attempt.catalog_id == "research.summary.run" for attempt in record.tool_attempts)
        assert len(services.workspace.search(query="Verified findings")) == 1
        verification = next(
            attempt for attempt in record.tool_attempts if attempt.catalog_id == "research.notes.read"
        )
        assert verification.result_json["content"] == saved.content
        assert saved.path in run["final_output"]
        if page_count > 1:
            assert all(
                item["status"] == "completed" for item in record.tool_attempts[-1].result_json["items"]
            )
        else:
            assert not any(attempt.catalog_id == "work.plan.create" for attempt in record.tool_attempts)


def test_unavailable_web_skill_sources_block_plan_without_saving(
    test_settings, stub_provider, monkeypatch,
):
    async def download(url):
        raise WebSourceUnavailable(url, "No readable page content.")

    needle = "# Web synthesis"
    stub_provider.tool_plans = [
        (needle, "create_work_plan", {"items": [{"id": "read", "title": "Read and synthesize"}]}),
        (needle, "acquire_research_source", {
            "kind": "web_page", "url": "https://example.com/empty", "title": None,
        }),
        (needle, "update_work_item", {
            "id": "read", "status": "blocked", "summary": "The page has no readable content.",
        }),
    ]
    stub_provider.reply = "The page is unavailable. No synthesis was saved."
    with TestClient(create_app(test_settings)) as client:
        configure_provider(client, stub_provider)
        services = client.app.state.services
        monkeypatch.setattr(services.source_downloads, "download_web_page", download)
        conversation = client.post("/api/agent/conversations", json={}).json()
        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={"content": "Use web-synthesis to read https://example.com/empty and save a guide."},
        )
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        record = services.runs.get(run["id"])
        assert record.tool_attempts[-1].result_json["items"][0]["status"] == "blocked"
        assert services.workspace.list_files() == []
        assert "No synthesis was saved" in run["final_output"]


def test_skill_api_edits_bundled_as_local_override_and_creates_custom_files(test_settings):
    with TestClient(create_app(test_settings)) as client:
        listed = client.get("/api/skills")
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()] == ["web-synthesis"]
        original = client.get("/api/skills/web-synthesis").json()
        assert original["source"] == "bundled"
        content = "# My web synthesis\n\nRetain source URLs.\n"
        response = client.put("/api/skills/web-synthesis", json={
            "content": content, "expected_revision": original["revision"],
        })
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["source"] == "local"
        assert saved["content"] == content
        assert saved["revision"] != original["revision"]
        assert client.get("/api/skills/web-synthesis").json() == saved
        assert SafeStorage(test_settings).read_text(
            test_settings.data_dir, "skills/web-synthesis.md",
        ) == content
        assert "# Web synthesis" in PromptRegistry().render_skills()
        assert "# My web synthesis" in client.app.state.services.prompts.render_skills()
        created = client.put("/api/skills/compare-notes", json={
            "content": "# Compare notes\n\nUse existing notes.", "expected_revision": None,
        })
        assert created.status_code == 200, created.text
        assert [item["name"] for item in client.get("/api/skills").json()] == [
            "compare-notes", "web-synthesis",
        ]
        assert client.app.state.services.workspace.list_files() == []
        assert client.get("/api/skills/missing").status_code == 404


def test_skill_api_prevents_stale_overwrites_and_rejects_invalid_edits(test_settings):
    with TestClient(create_app(test_settings)) as client:
        original = client.get("/api/skills/web-synthesis").json()
        _write_skill(test_settings, "web-synthesis.md", "# External edit")
        stale = client.put("/api/skills/web-synthesis", json={
            "content": "# Stale draft", "expected_revision": original["revision"],
        })
        assert stale.status_code == 409, stale.text
        assert client.get("/api/skills/web-synthesis").json()["content"] == "# External edit"
        assert client.put("/api/skills/web-synthesis", json={
            "content": "# Cannot create over existing skill", "expected_revision": None,
        }).status_code == 409
        latest = client.get("/api/skills/web-synthesis").json()
        for content in [" \n", "\u00e9" * (MAX_SKILL_BYTES // 2 + 1)]:
            response = client.put("/api/skills/web-synthesis", json={
                "content": content, "expected_revision": latest["revision"],
            })
            assert response.status_code == 400, response.text
        assert client.put("/api/skills/bad_name", json={
            "content": "# Invalid name", "expected_revision": None,
        }).status_code == 400
        assert client.put("/api/skills/new-skill", json={
            "content": "# Instructions", "tools": [{"name": "execute"}],
        }).status_code == 422
        assert client.get("/api/skills/web-synthesis").json() == latest


def test_skill_api_enforces_combined_budget_before_writing(test_settings):
    for index in range(2):
        _write_skill(test_settings, f"large-{index}.md", "x" * MAX_SKILL_BYTES)
    with TestClient(create_app(test_settings)) as client:
        response = client.put("/api/skills/large-third", json={
            "content": "x" * MAX_SKILL_BYTES, "expected_revision": None,
        })
        assert response.status_code == 400, response.text
        assert "Combined skill instructions" in response.text
        assert not (test_settings.data_dir / "skills" / "large-third.md").exists()


def test_failed_skill_replace_preserves_previous_file_and_cleans_temporary(test_settings, monkeypatch):
    _write_skill(test_settings, "custom.md", "# Original")
    with TestClient(create_app(test_settings)) as client:
        original = client.get("/api/skills/custom").json()
        replace = Path.replace

        def fail_replace(path, target):
            if Path(target).name == "custom.md":
                raise OSError("Disk replacement failed.")
            return replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_replace)
        response = client.put("/api/skills/custom", json={
            "content": "# Replacement", "expected_revision": original["revision"],
        })
        assert response.status_code == 400, response.text
        assert "Could not save skill" in response.text
        assert client.get("/api/skills/custom").json() == original
        assert [path.name for path in (test_settings.data_dir / "skills").iterdir()] == ["custom.md"]
