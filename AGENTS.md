# AGENTS.md — orientation for coding agents

Read this file first. It is the map of the repository. If you need more depth:

- [`docs/architecture.md`](docs/architecture.md) — subsystem-by-subsystem reference (what every module does, the run/event pipeline, the data model).
- [`docs/recipes.md`](docs/recipes.md) — copy-paste procedures for the changes people actually ask for (new endpoint, new agent tool, new frontend page, new provider kind, new test).
- [`README.md`](README.md) — product behaviour, configuration reference, setup. Read it when a task involves user-facing behaviour or config.

**You should not need to read the whole repo.** Use the tables below to jump straight to the 2–4 files your change touches.

---

## 1. What this project is

ScholarWeave is a **local-first research workspace**: you ingest PDFs, and agents built on the
**OpenAI Agents SDK (pinned at `0.19.4`)** search, read, and write notes about them.

One **single Uvicorn process** serves both the FastAPI API and the built React frontend.
Some state (the temporary web-page cache, the Ollama GPU lock) is process-local by design —
**never run it with multiple workers.**

```
Browser (React SPA)  ──HTTP /api──▶  FastAPI routers
                     ◀──SSE────────  (runs/{id}/events)
                                          │
                                     Feature services
                                     ┌────┴─────┐
                              Repositories   SDK runtime (Agents SDK)
                                     │            │
                                  SQLite      Tool runtime → documents / workspace / web
```

---

## 2. Golden rules

These are the constraints that will break the build or the product if you ignore them.

1. **`openai-agents==0.19.4` and `openapi-typescript==7.13.0` are exact pins.** Upgrading either is
   explicit, standalone compatibility work — never a side effect of another task.
   `backend/runtime/sdk_compat.py` asserts the version at startup.
2. **Never hand-edit `frontend/src/api/schema.generated.ts` or `frontend/openapi.json`.** They are
   generated. After *any* change to a router or a Pydantic schema, run
   `cd frontend && npm run generate:api` and commit both files. CI equivalent:
   `npm run check:api` fails on drift.
3. **Respect the layering.** Routers → services → repositories → SQLite. A router must not touch a
   repository or the DB directly, and a repository must not import a service. See §4.
4. **All dependencies come from the composition root.** `backend/bootstrap.py` constructs every
   service exactly once. Routers reach them via `Depends(services)`. Do not instantiate services,
   open DB sessions, or read `Settings()` inside a router or a tool handler.
5. **Never write files with bare `open()`/`Path.write_*` on user-facing paths.** Use
   `SafeStorage` (`backend/persistence/files.py`) or `WorkspaceService`. They enforce path-traversal
   protection, suffix allowlists, and size caps.
6. **`local_data/` and `workspace/` are runtime data and git-ignored.** Never commit them; never add
   fixtures there. Tests get isolated temp dirs from the `test_settings` fixture.
7. **Adding a new agent-callable tool requires edits in exactly two places** — the
   `APPLICATION_TOOLS` tuple in `backend/tools/catalog.py` *and* the dispatch dict in
   `ApplicationToolRuntime.invoke()` in `backend/tools/runtime.py`. Nothing statically enforces this
   pairing: a `catalog_id` with no handler raises `ValueError: Unknown application tool '<id>'`
   only when a model actually calls it. Verify with the snippet in
   [`docs/recipes.md`](docs/recipes.md#2-add-a-new-agent-callable-tool).
8. **Don't reintroduce the pre-SDK architecture.** `backend/tests/test_architecture.py` fails if any
   retired module name (`engine.py`, `nodes.py`, `workflows.py`, `registry.py`, `backend/schemas.py`,
   …) reappears at `backend/`.

---

## 3. Commands

Run these from the repository root unless noted. The virtualenv is `.venv/`.

| Task | Command |
| --- | --- |
| Backend, hot reload | `.venv/bin/uvicorn backend.app:app --reload --port 8000` |
| Frontend dev server | `cd frontend && npm run dev` |
| Backend tests (all) | `.venv/bin/pytest` |
| One backend test file | `.venv/bin/pytest backend/tests/test_sandbox.py` |
| One backend test | `.venv/bin/pytest backend/tests/test_sandbox.py::test_name` |
| Frontend tests | `cd frontend && npm test` |
| One frontend test file | `cd frontend && npx vitest run src/features/chat/chatTimeline.test.ts` |
| Typecheck + build frontend | `cd frontend && npm run build` |
| **Regenerate API contracts** | `cd frontend && npm run generate:api` |
| Verify contracts are fresh | `cd frontend && npm run check:api` |
| Health check | `curl -s http://127.0.0.1:8000/api/health` |

`pytest` config lives in `pyproject.toml` (`pythonpath = ["."]`, `testpaths = ["backend/tests"]`),
so plain `pytest` collects the whole backend suite.

**Minimum bar before you call a change done:**

- Touched backend Python → run the closest test file, plus `pytest backend/tests/test_architecture.py`.
- Touched a router or a Pydantic schema → `npm run generate:api`, then `npm run check:api`.
- Touched frontend → `npm test` and `npm run build` (build is the typecheck).

---

## 4. Layering rules

```
backend/<feature>/router.py      ← HTTP only: parse request, call service, shape response
        └─ Depends(services) → backend/bootstrap.py container
backend/<feature>/service.py     ← business logic, orchestration, cross-feature calls
backend/<feature>/repository.py  ← SQLAlchemy queries, the ONLY layer that opens sessions
backend/<feature>/models.py      ← SQLAlchemy ORM tables (persistence shape)
backend/<feature>/schemas.py     ← Pydantic request/response DTOs (wire shape)
```

Allowed import direction is strictly downward. Concretely:

- `router.py` may import `schemas.py` and `backend.api.dependencies`. It may **not** import
  `repository.py` or `models.py`.
- `service.py` may import its own `repository.py`, plus other features' *services*.
- `repository.py` may import its own `models.py` and `backend.core.errors`. Nothing else.
- **`models.py` (ORM) and `schemas.py` (Pydantic) are different types on purpose.** Convert at the
  service or router boundary. Do not return ORM objects from the API.

**Errors.** Raise the semantic exceptions from `backend/core/errors.py` — `NotFoundError` (→404),
`ConflictError` (→409), `ValidationError` (→400, carries `issues`). `backend/api/errors.py` maps
them to JSON responses globally. **Do not raise `HTTPException` from a service.**

---

## 5. Where things live

### Backend (`backend/`, ~28k lines)

| Package | Owns | Start reading at |
| --- | --- | --- |
| `core/` | `Settings`, health/settings routes, shared errors, JSON/text/time/vector helpers | `core/config.py` |
| `persistence/` | SQLAlchemy engine, `Base`, `JSONText`, schema cutover, `SafeStorage` | `persistence/database.py` |
| `providers/` | Provider profiles, model resolution, SDK client pool, Ollama, speech | `providers/runtime.py` |
| `agents/` | Blueprint schema, compiler (blueprint → SDK `Agent`), catalog, guardrails, templates | `agents/blueprint.py`, `agents/compiler.py` |
| `tools/` | The agent-callable tool surface, custom Python tools, the sandbox | `tools/catalog.py`, `tools/runtime.py` |
| `runtime/` | `ScholarWeaveContext`, SDK hooks, sessions, context compaction, SDK version pin | `runtime/context.py` |
| `runs/` | Run records, event persistence, SSE broker, execution orchestration | `runs/service.py`, `runs/events.py` |
| `conversations/` | Conversation records + SDK session history, conversation memory | `conversations/service.py` |
| `documents/` | PDF ingestion, OCR, vision clean-up, chunking, retrieval, figures | `documents/ingestion.py` |
| `research/` | DuckDuckGo / arXiv / Wikipedia search, web + PDF downloads, documents & artifacts routes | `research/search.py` |
| `workspace/` | Agent-writable Markdown files with FTS5 search and tags | `workspace/service.py` |
| `autonomous/`, `builder/`, `direct_agents/` | The three packaged agent experiences (extended work, agent builder, fixed research agents) | `autonomous/service.py` |
| `observability/` | Credential-redacted model-call audit log | `observability/llm_logging.py` |
| `tests/` | 26 test files, 208 tests | `tests/conftest.py` |

**Two files carry outsized weight — expect to open them often:**

- `backend/bootstrap.py` (259 lines) — the composition root. Every service and its wiring.
- `backend/tools/runtime.py` (1460 lines) — every agent tool handler. The dispatch table at
  `ApplicationToolRuntime.invoke()` (line ~89) is the index; jump from a `catalog_id` to its handler.

### Frontend (`frontend/src/`, ~19k lines)

React 18 + TypeScript 5.6 + Vite 6, Vitest for tests. **No Redux/Zustand/React Query and no
React Router** — plain hooks plus a custom ~100-line router in `src/app/router.tsx`.
Styling is plain CSS classes over design tokens in `src/shared/styles/tokens.css` (no Tailwind).

| Path | Owns |
| --- | --- |
| `app/App.tsx` | Path → page mapping (an if/else chain over `pathname`, lazy imports) |
| `app/AppShell.tsx` | Sidebar nav, chat history, command palette, theme switcher, topbar |
| `app/router.tsx` | `useLocation`, `useNavigate`, `Link`, `NavLink` |
| `api/client.ts` | `request<T>()`, `json()`, `apiUrl()`, `ApiError`. Base URL `/api` |
| `api/events.ts` | `subscribeToRun()` — SSE, plus the `RUN_EVENT_TYPES` list |
| `api/schema.generated.ts` | **Generated. Do not edit.** |
| `features/<name>/` | One folder per feature: `<Name>Page.tsx` + `api.ts` + CSS + tests |
| `shared/components/` | `Ui.tsx` (`PageHeader`, `Panel`, `Loading`, `EmptyState`, `ErrorNotice`, `StatusPill`), `Icons`, `MarkdownViewer`, `CommandPalette`, `ModelSelect`, `Chart`, `CodeBlock` |
| `shared/theme/` | 12 themes applied via `data-theme` on `<html>` |

Routes (`app/App.tsx`): `/` → chat (default), `/tools`, `/runs*`, `/papers*`, `/workspace*`, `/settings`.

Feature → page component mapping is *not* always obvious: papers live in `features/documents/PapersPage.tsx`
and settings live in `features/providers/SettingsPage.tsx`.

---

## 6. HTTP surface

All routes are mounted under the `/api` prefix (`Settings.api_prefix`), wired in `backend/app.py`.
Note that **path prefix does not always match the owning package** — `research/router.py` serves
`/documents`, `/web-sources`, and `/artifacts`; `conversations/router.py` serves three separate
conversation namespaces.

| Path prefix | Router file |
| --- | --- |
| `/health`, `/settings` | `core/router.py` |
| `/providers`, `/providers/speech/*` | `providers/router.py` |
| `/agents`, `/sdk/catalog` | `agents/router.py` |
| `/tools` | `tools/router.py` |
| `/conversations`, `/agent/conversations`, `/builder/conversations` | `conversations/router.py` |
| `/runs`, `/runs/{id}/events` (SSE) | `runs/router.py` |
| `/documents`, `/web-sources`, `/artifacts` | `research/router.py` |
| `/research-agents`, `/research-agent-conversations` | `direct_agents/router.py` |
| `/workspace/files` | `workspace/router.py` |

The authoritative, always-current list is `frontend/openapi.json`:

```bash
python3 -c "import json;d=json.load(open('frontend/openapi.json'));[print(m.upper().ljust(6),p) for p,o in sorted(d['paths'].items()) for m in o if m in ('get','post','put','delete')]"
```

---

## 7. How a chat turn actually runs

Knowing this flow explains most of the backend. Full detail in
[`docs/architecture.md`](docs/architecture.md#the-run-pipeline).

1. `POST /api/agent/conversations/{id}/messages` → `ConversationService` resolves the conversation
   and its SDK session.
2. A blueprint (`AgentBlueprint`) is compiled by `AgentCompiler.compile()` into a `CompiledAgent`:
   SDK `Agent` objects, resolved models, tools bound from `ToolCatalog`, handoffs, guardrails.
3. `RunService.create()` persists an `agent_runs` row; execution runs in a background task.
4. The SDK `Runner` executes. `ScholarWeaveRunHooks` + `runs/projector.py` translate SDK stream
   events into ScholarWeave events (`run.started`, `model.stream`, `tool.started`,
   `tool.completed`, `agent.completed`, `context.compacted`, `usage.updated`, …).
5. `PersistedRunEventSink` writes each event to `agent_run_events` **and** publishes it to the
   in-memory `EventBroker`.
6. `GET /api/runs/{id}/events?after=N` replays from the DB, then tails the broker over SSE.
7. The browser's `subscribeToRun()` feeds `chatStream.ts` (live text) and `chatTimeline.ts`
   (`buildTurnTimeline`, which rebuilds the identical timeline from events after a reload).

**Consequence for you:** events are the contract between backend and UI. If you add an event type,
add it to `RUN_EVENT_TYPES` in `frontend/src/api/events.ts` too, or the browser will silently
ignore it (`EventSource` only receives named events it subscribes to).

---

## 8. The agent tool surface

53 tools are exposed to models. Each is a `(catalog_id, tool_name, description, json_schema, strict)`
tuple in `APPLICATION_TOOLS` (`backend/tools/catalog.py`) plus a handler in the dispatch dict in
`ApplicationToolRuntime.invoke()` (`backend/tools/runtime.py`).

`catalog_id` is the internal key; `tool_name` is what the model sees.

| Group | `catalog_id` prefix | Examples (model-visible names) |
| --- | --- | --- |
| External research | `web.`, `arxiv.`, `wikipedia.`, `webpage.` | `search_web`, `search_arxiv`, `download_web_page` |
| Paper corpus | `documents.`, `research.`, `retrieval.` | `list_documents`, `ingest_paper`, `search_papers` |
| Workspace | `workspace.` | `write_workspace_file`, `append_workspace_markdown`, `ensure_paper_workspace` |
| Extended work | `extended.` | `create_extended_work_plan`, `save_extended_work_note` |
| Builder | `builder.` | `create_builder_todo_plan`, `finish_builder_run` |
| Authoring / meta | `agents.`, `function_tools.`, `sdk.`, `tools.` | `save_agent_blueprint`, `list_sdk_primitives` |
| Compute / context | `python.`, `conversation.`, `artifacts.`, `tool.` | `execute_python`, `search_conversation_memory` |

Print the live list any time:

```bash
.venv/bin/python -c "from backend.tools.catalog import APPLICATION_TOOLS
[print(f'{c:34} {n}') for c,n,*_ in APPLICATION_TOOLS]"
```

---

## 9. Testing

- Fixture `test_settings` (`backend/tests/conftest.py`) gives every test isolated `data_dir`,
  `workspace_dir`, and `frontend_dist_dir` under `backend/tests/_runtime/<test_name>/`.
- Fixture `stub_provider` (`backend/tests/stub_provider.py`) runs a real, deterministic
  OpenAI-compatible HTTP server on a free port. **Use it instead of mocking the SDK** — it is how
  the suite tests agent behaviour without a live model. It implements
  `POST /v1/chat/completions`, `GET /v1/models`, `POST /v1/embeddings`, supports SSE streaming, and
  lets you script tool calls and inspect received requests.
- Guard tests you should keep green: `test_architecture.py` (no retired modules, config defaults),
  `test_sdk_contract.py` (the pinned SDK still exposes every primitive and lifecycle argument the
  runtime relies on), `test_storage.py` + `test_sandbox.py` (path traversal, sandbox limits).

---

## 10. Gotchas

- **`backend/tools/runtime.py` is 1460 lines.** Read the `invoke()` dispatch table first, then jump
  to the single handler you need. Don't read it top to bottom.
- **`frontend/src/features/chat/ChatPage.tsx` is 1609 lines.** Streaming logic lives in the smaller,
  tested `chatStream.ts` / `chatTimeline.ts` — prefer changing those.
- **Schema changes are destructive.** `persistence/database.py` runs a *cutover*, not migrations:
  it backs up to `metadata.pre-sdk-*.sqlite3`, drops runtime tables, and recreates from ORM
  metadata, keyed on `SCHEMA_GENERATION`. There is no Alembic. Adding a column to an existing table
  will **not** migrate existing databases automatically — think before you do it.
- **Provider kinds are a closed set** (`backend/providers/schemas.py`): `ollama`, `openai`,
  `azure_openai`, `azure_foundry`, `openai_compatible`. Everything else (llama.cpp, vLLM,
  LM Studio) is reached through `openai_compatible`.
- **The Python sandbox is a guardrail, not a jail** (`tools/sandbox.py`): subprocess isolation,
  stdlib import allowlist, no sockets, scratch-only FS, CPU/memory caps.
- **There is no auth.** Bind to `127.0.0.1`. `local_data/metadata.sqlite3` stores provider API keys
  unencrypted. Never commit it, never log key material.
- **`docs/` currently holds only screenshots** plus the two documents referenced at the top of this
  file. There is no other hidden documentation.
