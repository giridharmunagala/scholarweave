# AGENTS.md

ScholarWeave is a local-first LLM research workspace. Keep it focused on:

1. chat with DuckDuckGo, arXiv, Wikipedia, PDF/HTML acquisition, and workspace read/write tools;
2. autonomous research driven by a small pending-work tracker;
3. paper notes, summaries, native PDF extraction, and OCR.

Read [docs/architecture.md](docs/architecture.md) for the runtime flow and
[docs/recipes.md](docs/recipes.md) for common changes.

## Non-negotiable rules

1. Keep `openai-agents==0.19.4` and `openapi-typescript==7.13.0` exactly pinned.
2. Never hand-edit `frontend/openapi.json` or `frontend/src/api/schema.generated.ts`. After changing
   a router or Pydantic API schema, run `cd frontend && npm run generate:api`.
3. Routers call services, services call repositories, and repositories own database sessions.
4. Construct services only in `backend/bootstrap.py`; access them in routers through
   `Depends(services)`.
5. Use `SafeStorage` or `WorkspaceService` for user-facing paths. Do not use bare file writes.
6. Never commit `local_data/` or `workspace/`.
7. A model-callable tool needs a catalog definition, a runtime handler, packaged JSON guidance, and
   a blueprint binding.
8. One Uvicorn worker only. The event broker, temporary page cache, and inference lock are
   intentionally process-local.

## Commands

Run from the repository root on Windows:

| Task | Command |
| --- | --- |
| Backend server | `.\.venv\Scripts\python.exe -m uvicorn backend.app:app --reload --port 8000` |
| Backend tests | `.\.venv\Scripts\python.exe -m pytest` |
| One backend file | `.\.venv\Scripts\python.exe -m pytest backend\tests\test_documents.py` |
| Frontend tests | `cd frontend; npm test` |
| Frontend typecheck/build | `cd frontend; npm run build` |
| Regenerate API | `cd frontend; npm run generate:api` |
| Check API drift | `cd frontend; npm run check:api` |

Minimum validation:

- Backend Python: closest test file plus `backend/tests/test_architecture.py`.
- Router or Pydantic schema: regenerate and check the API contract.
- Frontend: `npm test` and `npm run build`.

## Backend map

| Package | Owns | Start at |
| --- | --- | --- |
| `core` | settings, health, shared errors, HTTP dependencies | `core/config.py`, `core/http.py` |
| `persistence` | SQLite setup and safe files | `persistence/database.py` |
| `providers` | provider profiles, model clients, inference scheduling and model-call logs | `providers/runtime.py` |
| `agents` | blueprint-to-SDK compilation, run context and compaction | `agents/compiler.py`, `agents/context.py` |
| `tools` | fixed model tool surface and work-plan helpers | `tools/catalog.py`, `tools/runtime.py` |
| `runs` | execution, events, SSE, recovery | `runs/service.py` |
| `conversations` | chat records, session history and chat/deep-work blueprints | `conversations/service.py`, `conversations/autonomous.py` |
| `research` | DuckDuckGo, arXiv, Wikipedia, and PDF/HTML acquisition | `research/search.py` |
| `documents` | PDF ingestion, OCR, chunks, retrieval and versioned summaries | `documents/ingestion.py`, `documents/summaries.py` |
| `workspace` | Markdown files and search | `workspace/service.py` |
| `prompting` | packaged prompts and tool guidance | `prompting/registry.py` |

Do not create a new package unless it owns persistent data or a clearly independent runtime
boundary. Prefer adding a small module to an existing package.

## Layering

```text
router -> service -> repository -> SQLite
                   -> other services
```

- Routers parse HTTP input and shape responses.
- Services own behavior and orchestration.
- Repositories own SQLAlchemy sessions and queries.
- ORM models and Pydantic wire schemas remain separate.
- Services raise semantic errors from `backend/core/errors.py`, not `HTTPException`.

## Core flows

### Chat and Deep Work

Conversation routes compile a blueprint, create a run, and start the SDK runner. SDK stream events
are projected into persisted run events and published over SSE. Deep Work adds `create_work_plan`,
`read_work_plan`, and `update_work_item`; the run loop continues while an item is pending or in
progress.

### Papers

PDF acquisition stores the source, extracts native text, uses Tesseract when needed, optionally
repairs poor OCR with a vision model, and writes Markdown, a manifest, and searchable chunks.
Workspace `notes.md` and `summary.md` files are canonical; summary versions are immutable records.
Named paper folders are additive records; membership lives in document metadata, so organizing a
paper never moves its PDF or workspace files.

### Agent tools

`APPLICATION_TOOLS` in `backend/tools/catalog.py` is authoritative. Each tuple contains the
catalog ID, model-visible name, fallback description, strict JSON schema, and runtime handler.
Complete model guidance lives under `backend/prompting/defaults/tools/`.

## Important constraints

- `backend/tools/runtime.py` and `backend/runs/service.py` are large because they centralize,
  respectively, all tool handlers and the single run state machine. Read their dispatch/epoch
  sections rather than scanning top to bottom.
- Events are a backend/frontend contract. Add new named events to
  `frontend/src/api/events.ts`.
- Existing databases use a schema cutover strategy, not Alembic migrations. Read the persistence
  code before altering an existing table.
- There is no authentication. Bind to `127.0.0.1`.
- Tests use a real local OpenAI-compatible stub provider; prefer it over mocking the SDK.
