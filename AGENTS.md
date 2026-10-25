# ScholarWeave coding guide

Local-first assistance for **one researcher on one machine**: general chat, web/local lookup,
intent-led research, papers, notes, optional summaries, native PDF extraction and OCR. No accounts, teams, sharing, permissions,
or collaboration. Prefer reusing local evidence over acquiring or generating duplicate artifacts.

## Fast coding loop

1. Check `git status --short`; preserve existing edits. Choose the owning row below rather than
   scanning the repository. Read only the relevant heading in [recipes](docs/recipes.md), not the
   whole file. Consult the relevant section of [architecture](docs/architecture.md) only when
   crossing a runtime boundary or changing ownership/wiring.
2. Start with the owning function/component and its closest test. Follow router -> service ->
   repository, or catalog -> handler -> blueprint only as far as the behavior requires.
   Search for existing helpers before adding logic. Reproduce the behavior with the closest test.
3. Make one coherent change. Keep prompts, tool schemas, handlers, policies and bindings aligned.
4. Run the focused tests together; include the architecture guard for backend changes.
   Use the validation scope below instead of running every suite after each edit.
   Regenerate contracts for API changes.
   Check the final diff for accidental changes, generated drift, and unnecessary abstractions.
5. Simplify touched code by removing duplication/dead helpers, not by compressing readable logic or
   spreading one flow across more files. Do not bundle unrelated cleanup.

## Where to start

### Small-change exploration rules

- Use the map first. For a simple feature, the first read should usually be the owning code and
  its test, plus the relevant CSS for layout changes. Do not inventory the repository first.
- Search the named file for the control, endpoint or symbol, then read its enclosing function
  or component section. Avoid whole-file reads of large pages, services, tests or documentation.
- Widen only for a concrete dependency: a called helper, the source of an API field, a shared
  consumer, or a failing test. Once the owner, input/output and regression case are understood,
  implement; do not keep collecting background context.
- UI visibility/layout changes stay in the frontend unless the data contract must change.
  A missing API value starts in the service that supplies it, not in the renderer or ingestion
  pipeline by default. Existing stored text may be sufficient without re-ingestion.
- Do not read bootstrap, persistence, model harnesses, generated schemas or unrelated feature
  pages for a local presentation change. Inspect them only when the change actually touches
  their contracts. Do not delegate simple file discovery.
- Reuse file locations and facts already established in the current session. Re-read touched
  regions when needed to preserve concurrent edits, not the entire discovery trail.
- A broad dirty worktree is not a reason to investigate every changed file. Preserve unrelated
  work and inspect the diff only for files relevant to the task.

### Frontend and visible controls

Paths in this table are relative to `frontend/src/`. Tests are colocated with their owner.
Use `features/library.css` for Papers/Notes layout; do not start in shared styles unless the
change is shared. An entry lists alternatives, not a checklist of files to read.

| Change | Start here | Closest tests |
| --- | --- | --- |
| Papers columns, rows, abstract previews, filters, folders, reader toolbar | `features/documents/PapersPage.tsx` (search `renderRow`, `paper-table-head`, `papers-library-settings`, `reader-bar`) | `PapersPage.test.tsx` |
| Paper ordering / folder membership indexes | `features/documents/libraryModel.ts` | `libraryModel.test.ts` |
| PDF rendering / selection / zoom | `features/documents/PdfReader.tsx` | `PdfReader.test.tsx` |
| Paper notes / saved summaries / batch summaries | `features/documents/PaperNotes.tsx`, `PaperSummaryPanel.tsx`, `SummaryBatchPanel.tsx` | Corresponding component's `.test.tsx` |
| Notes browser / file editor | `features/workspace/WorkspacePage.tsx` | `WorkspacePage.ui.test.tsx` |
| Chat / run activity | `features/chat/ChatPage.tsx`, `RunActivity.tsx` | Relevant `ChatPage.*.test.tsx` |
| Deep Work / review controls | `features/deep-work/DeepWorkPage.tsx`, `features/reviews/LiteratureReviews.tsx` | Corresponding component's `.test.tsx` |
| Provider settings / model selection | `features/providers/SettingsPage.tsx`, `ProviderProfilesPanel.tsx`; shared picker: `shared/components/ModelSelect.tsx` | Corresponding component's `.test.tsx` |
| Tracks / linked repositories / writing | `features/tracks/TracksPage.tsx`, `LinkedRepositoryPanel.tsx`, `WritingPanel.tsx` | Corresponding component's `.test.tsx` |
| Morning Brief | `features/brief/BriefPage.tsx` | `BriefPage.test.tsx` |
| OCR batch progress / start / stop / history | `features/runs/RunsPage.tsx`, `features/documents/OcrBatchPanel.tsx` | Corresponding component's `.test.tsx` |
| Navigation / global search / shared settings drawer | `app/AppShell.tsx`, `app/CommandPalette.tsx`, `shared/components/SettingsDrawer.tsx` | Corresponding component's `.test.tsx` |

### Backend behavior

Paths below are relative to `backend/`. Tests live in `backend/tests/`.

| Change | Entry points | Closest tests |
| --- | --- | --- |
| Chat / Deep Work intent and composition | `conversations/turns.py`, `prompting/defaults/prompts/` | `test_research_modes.py`, `test_api.py` |
| Pending-plan continuation / recovery / SSE | `runs/service.py` (epoch/continuation sections), `runs/events.py` | `test_run_service.py`, `test_run_events.py` |
| Model loop / delegation / context | `agents/harness.py`, `agents/compiler.py`, `agents/context_budget.py` | `test_harness.py`, `test_agent_compiler.py`, `test_context_budget.py` |
| Agent tools | `tools/catalog.py`, `tools/runtime.py` (named handler), `tools/policy.py` | `test_agent_tools.py`, `test_tool_policy.py`, `test_prompting.py` |
| Workspace discovery / BM25 / files | `workspace/router.py`, `workspace/service.py`, `workspace/repository.py` | `test_storage.py`, `test_api.py` |
| Paper acquisition / web sources | `research/router.py`, `research/search.py`, `research/sources.py` | `test_research_search.py`, `test_source_downloads.py` |
| Paper abstract values in list / search / detail | `documents/service.py`: `document_abstracts`, `_opening_abstract`; only if stored input is missing: `documents/repository.py`: `opening_chunks` | `test_documents.py` (abstract cases) |
| Staged literature review / approvals / evidence | `research/reviews/service.py`, `research/reviews/jobs.py`, `research/reviews/discovery.py` | `test_literature_reviews.py` |
| PDF / OCR / retrieval / summaries | `documents/ingestion.py`, `documents/ocr.py`, `documents/summaries.py` | `test_documents.py`, `test_tesseract_ocr.py`, `test_summaries.py` |
| OCR batch queue / progress / cancellation / history | `documents/ocr_batches.py`, `documents/ocr_batch_repository.py` | `test_ocr_batches.py` |
| Providers / inference / settings | `providers/runtime.py`, `providers/inference.py`, `core/settings_service.py` | `test_providers.py`, `test_inference_scheduler.py`, `test_context_settings.py` |
| Storage / schema / wiring | `persistence/database.py`, `persistence/files.py`, `bootstrap.py` | `test_persistence_cutover.py`, `test_storage.py`, `test_architecture.py` |

Avoid reading all of the large tool runtime or run state machine.

### Launchers and local startup

Paths here are relative to the repository root.

| Change | Start here | Follow only if needed |
| --- | --- | --- |
| Windows browser launcher | `scripts/launch-scholarweave-web.ps1` | `scholarweave_tui/__main__.py` for flags/browser behavior |
| Windows terminal launcher / shortcut installer | `scripts/launch-scholarweave.ps1`, `install-windows-launcher.ps1` | Installer creates terminal, not browser, shortcuts |
| Backend reuse / startup / shutdown | `scholarweave_tui/launcher.py`: `BackendSession`; `scholarweave_tui/server.py`: `serve` | `backend/tests/test_tui_launcher.py` |
| Frontend dev server / backend proxy | `frontend/vite.config.ts`; commands in `frontend/package.json` | Vite reads `local_data/backend-runtime.json` (or configured data directory) |

Windows shortcuts point to launcher scripts; inspect only ScholarWeave shortcut targets when
asked to change desktop/Start/taskbar behavior. Do not assume a pinned shortcut opens the browser
rather than the terminal. Launchers use `--ensure-frontend` to reuse verified unchanged production
assets; `--build-frontend` forces a rebuild. Vite supports UI
hot updates, while backend Python changes require a restart and may interrupt active jobs.

## Invariants

- Native model harness: use the official `openai` client. Profiles explicitly choose
  `/v1/chat/completions` (default) or `/v1/responses`; never infer the API from a model name.
  Responses is stateless (`store=false`) with local history and application function tools only.
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

- The web UI keeps Auto/Quick chat separate from `/deep-work`: staged review decisions and
  Thorough research-task observability belong on that dedicated page. Reuse existing review
  and conversation APIs. Chat blueprints cannot start staged reviews; legacy API effort selectors
  and persisted active plans remain supported.
- Auto/Quick/Thorough effort is per-message, never a permanent conversation upgrade. Quick retains
  local sources and attachments. The **LLM** judges intent directly; no classifier calls or keyword
  triggers. General chat, clarification, and narrow lookups can finish without a plan.
  Once research is requested and scoped, create a small plan; pending/in-progress items keep the run
  going at any effort. Completed/blocked items need an honest outcome. Do not bypass an active plan.
- Summaries belong in chat unless saving is requested; legacy explicit `review` retains its artifact
  contract. Code execution and computational data analysis remain unsupported.
- Staged reviews approve scope/constraints and work limits once (papers screened, papers collected/read
  including references, and comparison passes). High/relevant originals and shared references are
  collected and read automatically; ambiguous matches are skipped visibly. Supplemental papers
  never expand references. Token/context safety remains internal; only exhausted work caps need extension.
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

Backend pytest uses the detected logical CPUs by default, up to 20 worker processes,
with work-stealing scheduling. Each worker/test has isolated temporary storage and provider sockets.
Use `-n 0` for a single test or debugging; use `-n 4` to lower resource use.

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

For frontend changes, run the affected feature's tests together from `frontend/`, for example:

```powershell
npm test -- src/features/chat
```

Vitest accepts multiple file or directory filters in one invocation. Include consumer tests when
changing shared components, hooks, or API helpers; do not select only newly added tests.
For a local component change, start with its test file rather than every feature's tests.
Broaden to the feature directory when multiple components are affected. A production frontend
build still type-checks and bundles the full application; this is not a reason to read every file.
On Windows, use `npm.cmd` if PowerShell blocks `npm.ps1`; do not change execution policy.
During repeated edits, use watch mode for that scope:

```powershell
npm test -- --watch src/features/chat
```

Run `npm run build` once after the coherent frontend change, not after each edit. It type-checks
application and test code and bundles the application; it does not execute tests.

Use the full affected suite for cross-cutting changes (shared test setup, routing, broad shared UI,
dependency upgrades, or backend bootstrap/persistence changes), when the impact cannot be bounded,
or when focused failures suggest wider regressions. Before a release, run the full backend suite
(`.\.venv\Scripts\python.exe -m pytest` from the repository root), full frontend suite (`npm test`
from `frontend/`), and frontend build. Keep all tests discoverable by these existing commands;
prefer behavior coverage over exhaustive component checks. Add tests for meaningful failure modes,
data/file safety, and core workflows, not exact labels, CSS classes, prompt wording, or static inventories.
Prune redundant and low-value presentation tests rather than maintaining one test per small component.
Avoid Cartesian integration matrices for independent settings: cover each meaningful outcome,
protocol and boundary without replaying the entire workflow for every combination.
Scripted model replies test plumbing, not the model's intent judgment or prompt quality.
Keep scale fixtures just beyond the actual boundary (pagination, budgets, retries), not arbitrary
hundreds of model calls. Use realistic spaced text for large-context fixtures rather than
pathological repeated-character strings unless tokenizer behavior is itself under test.
Keep one representative end-to-end workflow per feature/protocol and focused tests for its
meaningful failure modes; do not replay scope, discovery, ingestion and publication to check
a UI label or a setting. Test parsers directly rather than booting the full app for every
input variation. Backend tests must not invoke a frontend production build; run that once
separately when frontend or launcher build behavior changes.
Do not skip failing core regression tests to get a green run.
Documentation-only changes need no tests unless they change an executable contract.

Run the local backend with
`.\.venv\Scripts\python.exe -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 1102`.
