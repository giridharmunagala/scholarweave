# Architecture reference

Companion to [`AGENTS.md`](../AGENTS.md). This document explains *how the pieces fit together*.
Use [`recipes.md`](recipes.md) when you already know what you want to change.

- [1. Composition root](#1-composition-root)
- [2. Request lifecycle](#2-request-lifecycle)
- [3. Persistence](#3-persistence)
- [4. Providers and model resolution](#4-providers-and-model-resolution)
- [5. Agent blueprints and the compiler](#5-agent-blueprints-and-the-compiler)
- [6. The run pipeline](#the-run-pipeline)
- [7. Context budget and compaction](#7-context-budget-and-compaction)
- [8. Tools](#8-tools)
- [9. Documents and ingestion](#9-documents-and-ingestion)
- [10. Research and web sources](#10-research-and-web-sources)
- [11. Workspace](#11-workspace)
- [12. The three agent experiences](#12-the-three-agent-experiences)
- [13. Frontend](#13-frontend)

---

## 1. Composition root

`backend/bootstrap.py` is the only place services are constructed. `create_services(settings)`
returns an `ApplicationServices` dataclass holding ~28 singletons, and construction order encodes
the dependency graph:

```
Settings
  └─ create_session_factory()            # SQLAlchemy engine + schema cutover
       ├─ SettingsService                # DB-backed runtime settings overlay
       ├─ ProviderRepository             # + ensure_default_ollama()
       │    └─ ModelRuntime ─ SdkClientPool ─ ProfileModelResolver
       ├─ SafeStorage ─ WorkspaceService
       ├─ RetrievalService, ResearchSearchService
       ├─ DocumentRepository ─ DocumentOCR/Vision/Formatter/Figures ─ DocumentIngestion ─ DocumentService
       ├─ ToolCatalog + FunctionToolService (registers dynamic factory)
       ├─ GuardrailCatalog
       ├─ AgentCompiler(model_resolver, tool_catalog, guardrail_catalog, settings)
       ├─ AgentService, SdkSessionFactory, ConversationService, ConversationMemoryService
       ├─ EventBroker
       ├─ ApplicationToolRuntime(...)    # receives nearly every other service
       ├─ RunService(repo, sessions, tool_runtime, events, ...)
       └─ BuilderService / AutonomousAgentService / DirectAgentService / ProviderService
```

`backend/app.py` calls `create_services()`, stores the container on `app.state.services`, installs
global error handlers, includes every router under `/api`, and mounts the built frontend as a
catch-all SPA fallback. `ApplicationServices.close()` runs on lifespan shutdown and disposes HTTP
clients and the DB engine.

Routers obtain the container through a one-line dependency:

```python
# backend/api/dependencies.py
def services(connection: HTTPConnection) -> "ApplicationServices":
    return connection.app.state.services
```

```python
# any router
@router.get("", response_model=list[WorkspaceFileResponse])
def list_files(container=Depends(services)) -> list[WorkspaceFileResponse]:
    return [... for document in container.workspace.list_files()]
```

**Two `Settings` layers exist.** `backend/core/config.py` holds process settings loaded from
env/`.env` with the `SCHOLARWEAVE_` prefix. `SettingsService` overlays a subset that users can edit
at runtime through `PUT /api/settings`, persisted in the `app_settings` table. Fields marked ✅ in
the README's configuration table are the runtime-editable ones.

---

## 2. Request lifecycle

```
HTTP → router (backend/<feature>/router.py)
     → service (business logic; may call other services)
     → repository (only layer that opens a Session)
     → SQLite
```

Errors flow the other way through semantic exceptions rather than HTTP status codes:

| Raise (from `backend/core/errors.py`) | Becomes |
| --- | --- |
| `NotFoundError` | 404 `{"code": "not_found", "message": ...}` |
| `ConflictError` | 409 `{"code": "conflict", ...}` |
| `ValidationError(msg, issues=[...])` | 400, adds `"issues"` |
| `ApplicationError` (base) | 400 |
| `ProviderRuntimeError`, `DocumentProcessingError`, `StorageError` | 400 `{"code": "feature_error"}` |
| FastAPI `RequestValidationError` | 422, with `input`/`ctx` stripped |

Registered once in `backend/api/errors.py::install_error_handlers`. Services should never import
`fastapi`.

---

## 3. Persistence

SQLAlchemy 2.x ORM over a single SQLite file at `local_data/metadata.sqlite3`.

- `backend/persistence/database.py` defines `Base`, the `JSONText` type decorator (transparent
  JSON ↔ TEXT), and `create_session_factory()`.
- Each feature owns `models.py` with its tables. `_register_models()` imports all of them so
  `Base.metadata.create_all()` sees every table — **a new `models.py` must be added there or its
  table is never created.**
- Sessions are opened *only* inside repositories, via `with self._session_factory() as session:`.
  `sessionmaker` is configured `autoflush=False, expire_on_commit=False`, so objects stay usable
  after the session closes.

### Schema evolution: cutover, not migration

There is no Alembic. `_cut_over_schema()` compares the stored `schema_generation` (currently
`SCHEMA_GENERATION = 2`, in `app_settings`) against the code's value. On mismatch it:

1. writes a timestamped backup `metadata.pre-sdk-<ts>.sqlite3`,
2. renames `artifacts` and `document_chunks` aside to preserve them,
3. drops every table listed in `_RUNTIME_TABLES`,
4. renames/removes obsolete settings keys,
5. lets `create_all()` rebuild, then restores the preserved rows.

**Implication:** adding a column to an existing table will not appear in an existing developer
database. Either bump `SCHEMA_GENERATION` (destructive for runtime tables) or have the developer
delete `local_data/metadata.sqlite3`.

### Table ownership

| Package | Tables |
| --- | --- |
| `core` | `app_settings` |
| `providers` | `provider_profiles` |
| `agents` | `agent_definitions`, `agent_revisions` |
| `tools` | `function_tool_definitions`, `function_tool_revisions` |
| `conversations` | `conversations`, plus SDK-owned `sdk_sessions` / `sdk_session_items` |
| `runs` | `agent_runs`, `agent_run_items`, `agent_run_events`, `agent_run_interruptions` |
| `documents` | documents, `document_chunks`, `artifacts` |
| `direct_agents` | `direct_agent_conversations`, `paper_page_decisions`, `paper_summaries` |
| `workspace` | `workspace_entries` + the FTS5 virtual table `workspace_entries_fts` |

### Files

`SafeStorage` (`backend/persistence/files.py`) is the only sanctioned file writer. It resolves a
candidate path, asserts it stays under the given base directory, enforces suffix allowlists and byte
caps, and raises `StorageError` otherwise. Base directories come from `Settings`:
`data_dir`, `documents_dir`, `artifacts_dir`, `workspace_dir`.

---

## 4. Providers and model resolution

A **provider profile** is a persisted record of `kind`, `base_url`, optional API key, and a model
catalogue with per-model capabilities.

`ProviderKind` is a closed `Literal` in `backend/providers/schemas.py`:
`ollama | openai | azure_openai | azure_foundry | openai_compatible`. Local servers such as
llama.cpp, vLLM, and LM Studio are configured as `openai_compatible`. `ModelCapability` is
`chat | embedding | vision | tools | speech`.

Resolution chain for any capability:

```
ModelReference(provider_profile_id, model)
  → ModelRuntime.resolve()          # profile lookup, credentials, base_url, context window
  → ResolvedModel
  → SdkClientPool.get_client()      # cached AsyncOpenAI per profile
  → ProfileModelResolver.resolve_agent_model()
  → ResolvedAgentModel              # what the compiler binds onto an SDK Agent
```

Precedence when a reference is incomplete: explicit reference on the request → the agent's own
defaults → the application default from **Settings → Models**.

Ollama gets special handling: `ResolvedModel.agent_base_url` appends `/v1`, and a shared
`anyio.Lock` (created in `bootstrap.py`, passed to both `OllamaClient` and `ModelRuntime`)
serializes GPU work across the process. This lock is why multiple workers are unsupported.

Every model call is appended to `local_data/llm_calls.jsonl` by
`backend/observability/llm_logging.py` with credentials redacted — prompts and completions are not.

---

## 5. Agent blueprints and the compiler

An **`AgentBlueprint`** (`backend/agents/blueprint.py`) is the declarative, persisted, versioned
description of an agent graph. It is what the visual editor edits and what
`save_agent_blueprint` writes.

```
AgentBlueprint
├─ schema_version, sdk_version        # sdk_version must equal SUPPORTED_SDK_VERSION
├─ entry_agent_id
├─ agents:      list[AgentSpec]       # instructions, model, model_settings, output, tool_ids, guardrail ids
├─ tools:       list[ToolSpec]        # discriminated on `kind`:
│                                     #   FunctionToolSpec(catalog_id, config, needs_approval)
│                                     #   WebSearchToolSpec, FileSearchToolSpec
├─ handoffs:    list[HandoffSpec]     # source_agent_id → target_agent_id
├─ agent_tools: list[AgentToolSpec]   # expose an agent as another agent's tool (Agent.as_tool)
├─ guardrails:  list[GuardrailSpec]   # input | output | tool_input | tool_output
├─ run:         RunSettingsSpec       # max_turns, max_tool_concurrency, tracing_enabled
└─ session:     SessionPolicySpec     # history_max_items, messages_only
```

`AgentCompiler.compile(blueprint) -> CompiledAgent` (`backend/agents/compiler.py`):

1. validate all cross-references (agent ids, tool ids, guardrail ids, entry agent),
2. resolve each agent's model through `ProfileModelResolver`,
3. build each tool via `ToolCatalog.build_function_tool(spec)` — falling back to the dynamic factory
   registered by `FunctionToolService` for user-authored tools,
4. build guardrails from `GuardrailCatalog`,
5. construct `Agent[ScholarWeaveContext]` objects, wire handoffs and `as_tool` relations,
6. return `CompiledAgent` with `entry_agent`, `agents_by_id`, `resolved_models`, `run_config`,
   `max_turns`.

`AgentCompiler.validate(blueprint) -> tuple[str, ...]` returns issues without compiling; it backs
`POST /api/agents/validate` and the `validate_agent_blueprint` tool.

Instructions are wrapped at compile time by `backend/agents/instructions.py`:
`with_global_agent_instructions()` prepends `GLOBAL_AGENT_INSTRUCTIONS` plus
`current_system_information()` (current time in `Settings.user_timezone`, plus `Settings.user_profile`).
So **every agent implicitly receives timezone and user-profile context** — do not duplicate it in
per-agent prompts.

Agent records are versioned: `AgentRecord` (`agent_definitions`) with append-only `AgentRevision`
rows (`agent_revisions`) holding `blueprint_json` and `presentation_json` (canvas layout).
Runs reference a *revision*, so history stays reproducible.

---

## The run pipeline

A **run** is one execution of a compiled agent graph over one input.

### Execution

`RunService.create(compiled, input_value, ...)` inserts an `agent_runs` row (status `pending`) and
schedules `_execute()` as a background task; the HTTP request returns `202` immediately. Execution
is claimed by one boot generation and split into bounded SDK epochs. `agent_run_epochs` records
each segment, including its budget, usage, status, and terminal reason.

Inside `_execute()`, the SDK `Runner[ScholarWeaveContext]` streams the agent. Two mechanisms turn
SDK activity into ScholarWeave events:

- **`ScholarWeaveRunHooks`** (`backend/runtime/hooks.py`) implements SDK `RunHooks`:
  `on_agent_start/end`, `on_llm_start/end`, `on_tool_start/end`.
- **`backend/runs/projector.py`** converts SDK objects into JSON:
  `project_run_item()` for `MessageOutputItem`, `ToolCallItem`, `ToolCallOutputItem`,
  `HandoffOutputItem`, `ReasoningItem`, `ToolApprovalItem`; `project_stream_event()` maps raw
  stream events to `(event_type, payload)`.

### Context object

`ScholarWeaveContext` (`backend/runtime/context.py`) is threaded through every tool call. It carries
`run_id`, `conversation_id`, `tool_runtime`, `event_sink`, `receipts`, and a free-form `metadata`
dict, and exposes `await context.emit(event_type, payload)`. Tool handlers get it as their second
argument; `unwrap_scholar_context(tool_context)` retrieves it inside SDK tool wrappers.

### Events

`RunEventSink` is a Protocol with `emit`, `emit_transient`, and `emit_batch`.
`PersistedRunEventSink` writes to `agent_run_events` (monotonic `sequence` per run) *and* publishes
to the in-memory `EventBroker` (a `deque(maxlen=512)` per run plus subscriber queues, cleared on
terminal events). `emit_transient` broadcasts without persisting.

Event types, as consumed by the browser (`RUN_EVENT_TYPES` in `frontend/src/api/events.ts`):

```
run.started    run.resumed  run.recovered  run.interrupted  run.completed  run.failed
run.cancelled  run.paused   run.epoch.started  run.epoch.completed  run.item
agent.updated  agent.started  agent.completed  agent.failed  agent.superseded
model.stream   model.started  model.completed
tool.started   tool.completed  tool.failed  tool.attempt.started  tool.attempt.completed
tool.attempt.failed  tool.result.stored
context.compacted   context.compaction_failed
builder.todos.updated   goal.plan.updated   goal.blocked   goal.completed
handoff.completed   guardrail.result   guardrail.tripwire
approval.requested  approval.resolved   usage.updated
```

### Delivery

`GET /api/runs/{run_id}/events?after={sequence}` replays persisted events after `sequence`, then
tails the broker, emitting SSE frames (`id:` / `event:` / `data:`). Because history is persisted,
**a browser reload reconstructs the full timeline** — `buildTurnTimeline()` is deterministic over
the event list.

### Approvals

Tools declared `needs_approval` pause the run: an `agent_run_interruptions` row is created, an
`approval.requested` event fires, and the run reaches status `paused`. The client resolves via
`POST /api/runs/{run_id}/interruptions/{interruption_id}`, and the run resumes from persisted
`state_json`.

### Recovery and cancellation

At startup, pending runs are scheduled and interrupted conversation runs start a recovery epoch
from durable history. Any previously running epoch is marked `abandoned`; a tool attempt without a
persisted result becomes `unknown_outcome`. Recovery never blindly repeats such a write.

Cancellation is checked before the conversation lock, after acquiring it, before every tool, and
before every epoch. User cancellation uses SDK `immediate` cancellation and awaits the outer run
task, so active model streams and tool calls stop before the endpoint returns. The
`stop-and-answer` endpoint then starts a one-turn, tool-free run against the same conversation
session; its internal synthesis prompt is removed from durable history before completion. Shutdown
interrupts tasks without converting them to user cancellations, allowing startup recovery.

---

## 7. Context budget and compaction

`backend/runtime/context_budget.py` (683 lines) keeps long conversations inside the model window.

`create_context_budget_filter(settings, context_window_tokens_by_agent, agent_keys_by_agent)`
returns an async filter that the SDK calls before each model turn. When estimated tokens exceed
`agent_context_window_tokens × agent_context_high_water_ratio` (default 0.7), it summarizes older
history into a **checkpoint**, persists it as an artifact under
`local_data/artifacts/runs/<run_id>/checkpoints/<id>.json` (via
`ToolRuntime.store_context_checkpoint`), replaces the raw history with the checkpoint, and emits
`context.compacted`. If summarization fails, it emits `context.compaction_failed` and retains a
structured fallback rather than losing the current transcript.

The post-compaction target scales with the *selected model's* real context window, floored at
`agent_context_compaction_target_tokens` (default 8192). `Settings` validates at construction that
this floor is below the high-water mark.

Related: oversized **tool results** are bounded separately by
`ApplicationToolRuntime.bound_tool_result()` at `tool_result_max_tokens` (default 3000). The full
result is retained as an artifact and readable via the `read_tool_result` tool
(`tool.result.read`); truncation emits `tool.result.stored`.

---

## 8. Tools

### Registration

Two synchronized structures define every built-in tool:

```python
# backend/tools/catalog.py
APPLICATION_TOOLS: tuple[tuple[str, str, str, dict[str, Any], bool], ...] = (
    (
        "tools.search",                       # catalog_id  (internal key)
        "search_available_tools",             # name        (what the model sees)
        "Find tools available to the autonomous agent by keyword, name, or catalog ID.",
        _object_schema(                       # JSON Schema for arguments
            {"query": {"type": ["string", "null"], "description": "..."}},
            required=["query"],
        ),
        True,                                 # strict_json_schema
    ),
    ...
)
```

`create_tool_catalog()` turns each tuple into a `FunctionToolDefinition` whose factory builds an SDK
`FunctionTool`. The factory's `on_invoke_tool` is uniform — it parses arguments, unwraps the
context, and delegates:

```python
async def invoke(context: ToolContext[ScholarWeaveContext], raw_arguments: str) -> Any:
    arguments = json.loads(raw_arguments)
    scholar_context = unwrap_scholar_context(context)
    return await scholar_context.tool_runtime.invoke(catalog_id, arguments, scholar_context)
```

It is wrapped in `recoverable_tool_invoker` (`backend/tools/failures.py`) so tool exceptions become
model-readable failures rather than run crashes. Failure counts are tracked per information tool.
Three consecutive failures disable only that catalog tool for the remainder of the run; all other
tools, handoffs, and agent delegates remain available. A successful result resets that tool's counter.
`webpage.download` is deliberately exempt: source-level 4xx responses, non-HTML resources, and empty
pages return a successful `status: "unavailable"` result so one blocked site cannot remove the web
fetch capability from the run.

The second structure is the dispatch table in `ApplicationToolRuntime.invoke()`
(`backend/tools/runtime.py`, ~line 89), mapping every `catalog_id` to a handler:

```python
handlers = {
    "tool.results.read": self._read_tool_result,
    "tools.search": self._search_tools,
    "web.search": self._search_web,
    ...
}
handler = handlers.get(catalog_id)
if handler is None:
    raise ValueError(f"Unknown application tool '{catalog_id}'.")
```

After dispatch, `invoke()` uniformly applies safety limits (`consume_tool_safety_limit`), bounds the
result, marks progress, emits feature-specific events for builder/extended-work tools, and emits
`tool.application_completed`. **Handlers therefore stay small: take `(arguments, context)`, return a
JSON-serializable value.** They may be sync or async.

`ApplicationToolRuntime` receives its collaborators (documents, workspace, retrieval, research,
storage, agents, function tools, catalog, direct agents, conversation memory) via constructor
injection in `bootstrap.py` — a handler uses `self._workspace`, `self._documents`, etc.

### User-authored tools

`FunctionToolService` (`backend/tools/service.py`) persists user-written Python tools
(`function_tool_definitions` / `function_tool_revisions`, versioned like agents), validates their
code and schema, and exposes `dynamic_factory`, registered on the catalog at bootstrap:

```python
tool_catalog.register_dynamic_factory(function_tools.dynamic_factory)
```

So a `FunctionToolSpec` with an unknown `catalog_id` falls through to a user tool. Their code runs
in the sandbox.

### Sandbox

`backend/tools/sandbox.py` executes Python in an isolated subprocess: stdlib import allowlist
(`Settings.python_tool_allowed_imports`), blocked sockets, scratch-only filesystem, no subprocesses,
CPU timeout (`python_tool_timeout_seconds`), memory cap (`python_tool_memory_mb`), file-size caps.
Disable entirely with `SCHOLARWEAVE_PYTHON_TOOL_ENABLED=false`. Treat it as a guardrail against
runaway tool code, not as a security boundary against an attacker.

---

## 9. Documents and ingestion

`DocumentIngestion.ingest(document_id, ...)` (`backend/documents/ingestion.py`) is the pipeline:

1. **Load** — `DocumentRepository.get_details()`; find the `source_pdf` artifact under
   `Settings.documents_dir`.
2. **Resolve enhancement models** — if LLM clean-up is on, `VisionEnhancer.resolve_enhancement_model()`
   and `resolve_quality_model()`.
3. **Extract** — `DocumentOCR.extract_pages(pdf_path, force_ocr=..., retain_page_images=..., progress=...)`.
   Each page uses its native PDF text when it has at least `pdf_min_text_chars`; otherwise it is
   rendered and passed to Tesseract. Only an explicit user `force_ocr` request bypasses that rule.
4. **Figures** — `FigureExtractor.extract()` (in a worker thread), grouped onto pages by page number.
5. **Enhance or annotate** — with enhancement on, `VisionEnhancer.enhance_pages()` triages poor pages
   and rewrites them with a vision model; otherwise
   `DocumentFormatter.append_missing_figure_references()` runs.
6. **Persist** — `_persist_document_content()` writes extracted text artifacts, chunks the text
   (`max_chunk_chars`, `max_chunks_per_document`), stores `document_chunks` with embeddings via
   `RetrievalService`, and records the extraction mode.

Progress is reported throughout via a `ProgressCallback`, surfaced over SSE at
`GET /api/documents/{id}/ingest/events` and driving the Papers UI.

`RetrievalService` (`backend/documents/retrieval.py`) owns chunk storage plus keyword and vector
search, merged and bounded by `retrieval_max_context_chars`. It backs the `search_papers` and
`read_document_chunks` tools.

`GET /api/documents/{id}/ingestion-options` reports per-document page statistics and the recommended
mode (`embedded` vs `ocr`) before you commit to a run.

---

## 10. Research and web sources

`backend/research/search.py` provides direct DuckDuckGo, arXiv, and Wikipedia search, each
behind an async rate limiter configured from `Settings`:
`web_search_max_requests_per_session` (per agent session, hard max 100),
`arxiv_search_requests_per_minute` (max 20), `wikipedia_search_requests_per_minute`.

`backend/research/sources.py` handles downloads. `SourceDownloadService` fetches PDFs (auto-creating
a document and triggering ingestion) and web pages. Fetched pages go into a **process-local**
temporary cache bounded by `max_temporary_web_sources` and `web_source_ttl_minutes`, capped at
`max_web_source_bytes` — another reason the app must run as a single process. Cached pages are
exposed as `/api/web-sources` and to agents as `download_web_page` / `read_downloaded_web_page` /
`search_downloaded_web_page`.

Downloaded content is untrusted input and can carry prompt-injection payloads.

---

## 11. Workspace

`workspace/` on disk is the agent-writable Markdown area, with a canonical layout:

```
workspace/
├── notes/<uuid>/note.md
└── papers/<document-id>/
    ├── summary.md
    └── notes.md
```

`WorkspaceService` mediates all access through `SafeStorage` (suffix allowlist, traversal
protection, `max_workspace_file_bytes`). `WorkspaceRepository` mirrors every entry into
`workspace_entries` and an FTS5 virtual table `workspace_entries_fts`, rebuilt at startup if the row
counts diverge — so full-text and tag search stay consistent even if files change out of band.

Agents are steered toward `ensure_paper_workspace` to obtain the canonical folder for a paper, and
toward `replace_workspace_markdown` / `append_workspace_markdown` over wholesale rewrites.

---

## 12. The three agent experiences

| Experience | Service | Conversation namespace | What it is |
| --- | --- | --- | --- |
| **Chat / autonomous** | `autonomous/service.py` | `/api/agent/conversations` | One research coordinator with a scoped research/workspace tool set, durable goal controls, bounded epochs, and context compaction. |
| **Builder** | `builder/service.py`, `builder/todos.py` | `/api/builder/conversations` | An agent that authors *other* agents. Drives an explicit TODO plan (`builder.todos.*`) and must finish with `finish_builder_run` plus a save receipt. |
| **Direct research agents** | `direct_agents/service.py` | `/api/research-agent-conversations` | Fixed, single-purpose agents listed at `GET /api/research-agents`: summary agent, open-areas agent, per-paper Q&A, and a paper cleaner that records keep/no-keep page decisions. |

Generic `/api/conversations` serves conversations created directly from a saved blueprint.

Goal state is persisted in `agent_goal_states` through three coordinator controls:
`update_goal_plan`, `request_clarification_or_block`, and `finish_goal`. The UI renders
`goal.plan.updated`, `goal.blocked`, and `goal.completed` events. Research mode does not expose
agent authoring, custom Python, shell, browser, LSP, or computer-use tools.

---

## 13. Frontend

### Stack

React 18.3, TypeScript 5.6, Vite 6.4, Vitest 3.2, `openapi-typescript` 7.13 (pinned).
Notable *absences*: no Redux/Zustand/React Query, no React Router, no Tailwind, no component
library. State is `useState`/`useRef`; data fetching is direct `fetch` wrappers.

### Structure

```
src/
├── app/        App.tsx (path→page), AppShell.tsx (nav/palette/theme), router.tsx (custom router)
├── api/        client.ts, events.ts (SSE), schema.generated.ts (GENERATED)
├── features/   agents/ chat/ dashboard/ direct-agents/ documents/ providers/ runs/ tools/ workspace/
└── shared/     components/, theme/ (12 themes), styles/ (tokens.css, base.css, components.css, layout.css)
```

Each feature folder holds `<Name>Page.tsx` (default export, lazy-loaded), `api.ts`, feature CSS, and
`*.test.ts(x)`.

### API layer

`src/api/client.ts` is ~60 lines: `request<T>(path, init)` prefixes `/api`
(overridable with `VITE_API_BASE`), sets JSON headers unless the body is `FormData`, returns
`undefined` on 204, and throws `ApiError(status, message, details)` on failure — reading the
backend's `{code, message, issues}` shape. Helpers: `json(method, body)`, `apiUrl(path)`,
`apiWebSocketUrl(path)`.

Feature APIs are plain objects over generated types:

```ts
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type Conversation = components['schemas']['ConversationResponse'];

export const chatApi = {
  list: () => request<Conversation[]>('/agent/conversations'),
  create: (title: string, modelReference: ModelReference) =>
    request<Conversation>('/agent/conversations', json('POST', { title, model_reference: modelReference })),
};
```

### Streaming

`subscribeToRun(runId, after, onEvent, onError)` opens an `EventSource` on
`/api/runs/{id}/events?after=N`, registers a listener for **every** name in `RUN_EVENT_TYPES`
(plus `onmessage`), drops events whose `sequence` is not greater than the cursor, and returns an
unsubscribe function.

Two consumers, both pure and unit-tested:

- **`chatStream.ts`** — `applyChatStreamEvent(state, event)` folds events into
  `{ reasoning, assistant, tools, events }` for the live view.
- **`chatTimeline.ts`** — `buildTurnTimeline(events)` reconstructs an ordered
  `ReasoningStep | ToolStep | HandoffStep | AgentStep` list with per-step durations. Because it is a
  pure function of the persisted event log, live and post-reload timelines are identical.

Keep logic in these two modules rather than in `ChatPage.tsx` (1609 lines).

### Contract generation

```
backend FastAPI app.openapi()
  → scripts/export_openapi.py       → frontend/openapi.json
  → openapi-typescript              → frontend/src/api/schema.generated.ts
```

`npm run generate:api` runs both steps; `npm run check:api`
(`scripts/check_api_contracts.py`) regenerates into a temp dir and fails with a diff if the
committed files are stale. `export_openapi.py` builds the app against temporary data directories,
so it never touches your real `local_data/`.
