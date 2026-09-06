# ScholarWeave coding guide

Local-first research for **one researcher on one machine**: sourced chat, intent-led Deep Work,
papers, notes, summaries, native PDF extraction and OCR. No accounts, teams, sharing, permissions,
or collaboration. Prefer reusing local evidence over acquiring or generating duplicate artifacts.

## Fast coding loop

1. Check `git status --short`; preserve existing edits. Choose the owning row below rather than
   scanning the repository. Read [recipes](docs/recipes.md) for the change procedure and the relevant
   section of [architecture](docs/architecture.md) when crossing a runtime boundary.
2. Trace router -> service -> repository, or catalog -> handler -> blueprint. Search for existing
   helpers and tests before adding files. Reproduce the behavior with the closest test.
3. Make one coherent change. Keep prompts, tool schemas, handlers, policies and bindings aligned.
4. Run the focused tests together, then the architecture guard. Regenerate contracts for API changes.
   Check the final diff for accidental changes, generated drift, and unnecessary abstractions.
5. Simplify touched code by removing duplication/dead helpers, not by compressing readable logic or
   spreading one flow across more files. Do not bundle unrelated cleanup.

## Where to start

Paths below are relative to `backend/`.

| Change | Entry points | Closest tests |
| --- | --- | --- |
| Chat / Deep Work intent and composition | `conversations/turns.py`, `prompting/defaults/prompts/` | `test_research_modes.py`, `test_api.py` |
| Pending-plan continuation / recovery / SSE | `runs/service.py` (epoch/continuation sections), `runs/events.py` | `test_run_service.py`, `test_run_events.py` |
| Model loop / delegation / context | `agents/harness.py`, `agents/compiler.py`, `agents/context_budget.py` | `test_harness.py`, `test_agent_compiler.py`, `test_context_budget.py` |
| Agent tools | `tools/catalog.py`, `tools/runtime.py` (named handler), `tools/policy.py` | `test_agent_tools.py`, `test_tool_policy.py`, `test_prompting.py` |
| Workspace discovery / BM25 / files | `workspace/router.py`, `workspace/service.py`, `workspace/repository.py` | `test_storage.py`, `test_api.py` |
| Paper acquisition / web sources | `research/router.py`, `research/search.py`, `research/sources.py` | `test_research_search.py`, `test_source_downloads.py` |
| PDF / OCR / retrieval / summaries | `documents/ingestion.py`, `documents/ocr.py`, `documents/summaries.py` | `test_documents.py`, `test_tesseract_ocr.py`, `test_summaries.py` |
| Providers / inference / settings | `providers/runtime.py`, `providers/inference.py`, `core/settings_service.py` | `test_providers.py`, `test_inference_scheduler.py`, `test_context_settings.py` |
| Storage / schema / wiring | `persistence/database.py`, `persistence/files.py`, `bootstrap.py` | `test_persistence_cutover.py`, `test_storage.py`, `test_architecture.py` |

Tests live in `backend/tests/`. Frontend features live in `frontend/src/features/`; use their
colocated tests. Avoid reading all of the large tool runtime or run state machine.

## Invariants

- Native runtime: every provider uses the official `openai` client and `/v1/chat/completions` only.
  Keep `openapi-typescript` exactly **7.13.0**.
- Routers parse/shape HTTP; services orchestrate and raise semantic `core/errors.py` errors;
  repositories own SQLAlchemy sessions/queries. ORM records and Pydantic wire models stay separate.
- Construct services in `backend/bootstrap.py`; routers use `Depends(services)`.
- User-facing paths go through `SafeStorage` or `WorkspaceService`. Never commit `local_data/` or
  `workspace/`. Never write user files through bare paths.
- A model tool needs a six-field `APPLICATION_TOOLS` entry, runtime handler, packaged JSON guidance,
  operation policy, and binding in `conversations/turns.py`. Strict object schemas forbid extra keys
  and require every property; optional values are nullable.
- One Uvicorn worker, bound to `127.0.0.1`: no authentication; event broker, inference locks, caches,
  and workspace mutation locks are process-local.
- Existing database tables use explicit schema cutover, not Alembic. Read persistence code before
  changing a table; `create_all()` is not a migration.
- Named SSE events must also appear in `frontend/src/api/events.ts`.

## Research behavior

- Deep Work is a capability, not a command to execute on every turn. The **LLM** judges intent from
  conversation context; no keyword triggers. Clarification/discussion can finish without a plan.
  Once research is requested and scoped, create a small plan; pending/in-progress items keep the run
  going. Completed/blocked items need an honest outcome. Do not bypass an active plan.
- Delegation is explicit through `agent_tools`; workers see only the request, not the coordinator
  transcript. Maximum depth: coordinator -> sub-agent -> nested helper.
- Canonical paper `notes.md` and `summary.md` are workspace files; summary versions are immutable.
  Named paper folders are metadata membership, never physical file moves.
- Discovery: existing `GET /api/documents` lists papers; workspace endpoints list notes/summaries,
  search, and inspect/refresh the index. Use the existing SQLite FTS5 inverted index with BM25, not a
  second JSON index or model call. Application mutations index automatically; external edits require
  explicit refresh. Workspace excerpts are leads, not PDF source evidence.
- Behavior tests use the real local OpenAI-compatible stub provider, not a mocked harness.

## Validation (PowerShell, repository root)

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_storage.py backend\tests\test_architecture.py
```

Replace/add the closest test files from the table. For router/Pydantic changes, never hand-edit
`frontend/openapi.json` or `frontend/src/api/schema.generated.ts`; regenerate and check:

```powershell
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
Set-Location frontend
npm run generate:api
npm run check:api
```

Frontend changes also require `npm test` and `npm run build` from `frontend/`.
Run the local backend with
`.\.venv\Scripts\python.exe -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000`.
