# Architecture

ScholarWeave is one local FastAPI application with a React frontend, SQLite metadata, and local
files. It is not a distributed system.

```text
React SPA
  | HTTP + SSE
FastAPI routers
  |
feature services
  |--------------------|
repositories/SQLite    OpenAI Agents SDK
                       |
                       fixed application tools
                       |
                       documents, workspace, source search, web
```

## Composition

`backend/bootstrap.py` is the composition root. It creates one instance of every repository,
service, model runtime, SDK compiler, tool runtime, run service, and event broker. Routers obtain
that container through `Depends(services)`.

The backend packages are ownership boundaries inside the same process:

- `core`, `api`, and `persistence`: platform concerns;
- `providers`, `agents`, `runtime`, `tools`, and `runs`: model execution;
- `conversations` and `autonomous`: chat and Deep Work;
- `research`, `documents`, `workspace`, and `summaries`: research data;
- `prompting` and `observability`: packaged instructions and redacted diagnostics.

## Run pipeline

1. A conversation message route resolves its stored model reference.
2. `AutonomousAgentService` builds a small research or Deep Work blueprint.
3. `AgentCompiler` resolves models and turns the blueprint into SDK `Agent` objects and function
   tools.
4. `RunService` creates a persisted run and executes the SDK runner in the background.
5. SDK hooks and the projector translate stream activity into named events.
6. `PersistedRunEventSink` stores events in SQLite and publishes them to the in-process broker.
7. `/api/runs/{id}/events` replays stored events and then tails the broker over SSE.
8. The React chat rebuilds the same transcript from live or replayed events.
9. A terminal run writes a redacted operational snapshot under `local_data/run_logs/`.

Runs are split into bounded epochs. Turn, epoch, wall-clock, and tool-call limits prevent an
autonomous task from running forever.

## Autonomous work

Deep Work exposes exactly three plan tools:

- `create_work_plan(items)`
- `read_work_plan()`
- `update_work_item(id, status, summary)`

The plan is run-scoped metadata. Items begin as `pending`; the model can mark them `in_progress`,
`completed`, or `blocked`. Completed and blocked items require a result or blocker summary.

When an SDK epoch returns, `RunService` checks the plan. If no plan exists or open items remain, it
persists that epoch and starts another with the open items in the continuation instruction. Normal
completion is allowed only after no item is pending or in progress.

## Tool surface

`backend/tools/catalog.py` declares the fixed tool contract. `ApplicationToolRuntime.invoke()` in
`backend/tools/runtime.py` dispatches those catalog IDs to handlers and centralizes:

- timeouts and read retries;
- result-size limits and result references;
- receipts and activity metadata;
- tool events and error classification.

The useful surface is intentionally small:

- DuckDuckGo, arXiv, and Wikipedia search through one source-search tool;
- PDF/HTML acquisition and reading;
- local paper search/read;
- metadata-only paper folder organization;
- workspace search/read/write/append;
- paper notes and summary persistence;
- bounded large-result reads;
- Deep Work plan tracking.

Prompt and tool guidance is packaged and read-only at runtime.

## Documents and OCR

PDF ingestion is application-owned:

1. Save and retain the source PDF.
2. Inspect every page's native text.
3. Render and OCR pages whose text layer is inadequate, or all pages when forced.
4. Optionally classify and repair poor OCR with configured vision models.
5. Build extracted Markdown, a page manifest, and searchable chunks.
6. Store generated artifacts and update the document record atomically.

Tesseract availability includes both its executable and configured language data. `TESSDATA_PREFIX`
is normalized to the discovered language-data directory before OCR calls.

Acquiring a paper also creates its workspace directory and canonical `notes.md` and `summary.md`
files. Summary saves create immutable versions and update canonical `summary.md`. Named folder
records live in SQLite, while a paper's optional `folder_id` lives in its document metadata.
Assignment therefore does not move or rewrite the source PDF or workspace files.

## Storage

SQLite stores metadata and run history. User-visible Markdown lives in `workspace/`; source PDFs and
generated artifacts live in `local_data/`.

All user-controlled paths go through `SafeStorage` or `WorkspaceService`, which enforce resolved
roots, suffix allowlists, and size caps.

## Context compaction

Context compaction preserves every original user message verbatim. Older model and tool activity is
replaced with a bounded checkpoint containing evidence, references, caveats, tool receipts, paper
activity, and the current work plan. This keeps long runs usable without losing the user's request
or durable work state.

## Schema evolution

There is no Alembic. SQLite startup creates missing tables, while incompatible runtime-schema
changes use the explicit cutover generation in `backend/persistence/database.py`. Adding a column
to an existing table requires deliberate cutover work; `create_all()` alone will not migrate it.

## Deployment constraints

- Use one Uvicorn worker.
- Bind to `127.0.0.1`; there is no authentication.
- Provider credentials are stored locally and unencrypted.
- Keep the exact SDK and OpenAPI generator pins documented in `AGENTS.md`.
