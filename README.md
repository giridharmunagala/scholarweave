# ScholarWeave

ScholarWeave is a local-first research workspace for one researcher on one local machine. It has
three jobs:

1. Talk to an LLM that can search DuckDuckGo, arXiv, and Wikipedia, read and write workspace files,
   and acquire PDF or HTML sources.
2. Run longer research autonomously against a small tracked work plan until every item is completed or blocked.
3. Keep research-paper notes and summaries, with native PDF extraction and Tesseract OCR fallback.

Everything runs in one FastAPI/Uvicorn process. The same process serves the API, streams run events,
and serves the built React application.

The library is intentionally personal: there are no accounts, teams, sharing, permissions, or
collaborative editing. Conversations, papers, folders, notes, summaries, and runs all implicitly
belong to the local researcher.

## What remains

### Research chat

The chat agent can:

- search the general web through DuckDuckGo, papers through arXiv, and encyclopedic background
  through Wikipedia;
- download a public PDF or HTML page;
- search and read acquired papers and web pages;
- search, read, write, and append Markdown workspace files;
- save paper notes and versioned summaries.

Large tool results are stored locally and can be read back in bounded slices.

### Deep Work

Deep Work uses the same research tools plus three tracker operations:

- `create_work_plan`
- `read_work_plan`
- `update_work_item`

The run loop feeds open items back to the model and continues in bounded epochs. A run finishes only
when all items are `completed` or `blocked`, or when the configured epoch, turn, or time budget is
exhausted. A focused worker remains available for genuinely independent research tracks.

### Papers and OCR

Papers can be uploaded or downloaded from a URL. Ingestion:

1. retains the original PDF;
2. reads its native text layer;
3. uses Tesseract selectively when text is missing, or for every page when OCR is forced;
4. optionally uses a configured vision model to repair poor OCR;
5. stores page text, searchable chunks, extracted Markdown, and a manifest.

Every acquired paper has canonical `summary.md` and `notes.md` files in the workspace. Saving a
reviewed summary also creates an immutable summary version. Papers can be grouped into named folders;
folder assignment changes document metadata only and never moves the source PDF or workspace files.

## Setup

Requirements:

- Python 3.12+
- Node.js 20+
- Tesseract with the configured language data for OCR
- Ollama or another OpenAI-compatible model provider

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Set-Location frontend
npm install
npm run build
Set-Location ..

.\.venv\Scripts\python.exe -m uvicorn backend.app:app --reload --port 8000
```

Open `http://127.0.0.1:8000`. ScholarWeave has no authentication and should remain bound to
localhost.

## Development

```powershell
# Backend
.\.venv\Scripts\python.exe -m pytest

# Frontend
Set-Location frontend
npm test
npm run build

# After changing a router or Pydantic API schema
npm run generate:api
npm run check:api
```

One dependency pin is deliberate and must not drift as a side effect:

- `openapi-typescript==7.13.0`

The agent runtime is native: ScholarWeave uses the official `openai` client and only the
`/v1/chat/completions` API, so any OpenAI-compatible endpoint (llama.cpp, Ollama, Azure OpenAI,
OpenRouter, and similar) works without a vendor agent framework.

## Configuration and data

Provider profiles, application settings, conversations, runs, and document metadata are stored in
`local_data/metadata.sqlite3`. Provider API keys are local and unencrypted.

User content is split between:

| Path | Contents |
| --- | --- |
| `workspace/` | User-visible Markdown notes, summaries, and research files |
| `local_data/documents/` | Original PDFs |
| `local_data/artifacts/` | Extracted text, manifests, bounded tool results, and checkpoints |
| `local_data/run_logs/` | Credential-redacted operational run snapshots |

`local_data/` and `workspace/` are ignored by Git.

## Why the backend still has packages

Package count is not a product feature, but a few boundaries prevent unsafe coupling:

| Package | Single responsibility |
| --- | --- |
| `core` | settings, health, shared errors and small utilities |
| `persistence` | database and safe local file access |
| `providers` | provider profiles and model clients |
| `agents` | the native model/tool harness plus blueprint compilation, sessions and compaction |
| `tools` | the fixed model-callable tool catalog and handlers |
| `runs` | run lifecycle, persisted events, and SSE |
| `conversations` | chat records and session history |
| `autonomous` | research/deep-work blueprints and the work tracker |
| `research` | DuckDuckGo, arXiv, and Wikipedia search plus PDF/HTML acquisition routes |
| `documents` | PDF storage, extraction, OCR, chunks, and retrieval |
| `workspace` | safe Markdown files and workspace search |
| `summaries` | immutable paper summary versions |
| `prompting` | read-only packaged prompts and tool descriptions |
| `observability` | redacted model/run logs |
| `api` | dependency injection and global API errors |

These are code ownership boundaries, not separately deployed services. The removed product surfaces
included speech, Prompt Studio, runtime skills, figure extraction, and dedicated note/summary
sub-agents.

See [AGENTS.md](AGENTS.md) for the repository map and
[docs/architecture.md](docs/architecture.md) for the runtime flow.
