# ScholarWeave

ScholarWeave is a personal research workspace for reading papers, exploring ideas, and organizing
notes and summaries. Research chat connects your questions to sources, while Deep Work follows a
tracked plan for longer investigations.

Connect Ollama, your own OpenAI-compatible server, or a hosted service with your own API key.
Your library is stored on your machine; model inference runs with the provider you choose.

![ScholarWeave paper library](docs/screenshots/papers.png)

## Features

### Research Chat And Deep Work

- Search the web, arXiv, and Wikipedia; acquire PDF papers and web sources.
- Ask questions about papers, compare findings, and follow citations back to the evidence.
- Choose a response style: **Follow my request**, **Learn / ask**, **Explain in depth**, or **Review + save**.
- Use **Deep Work** for longer investigations with a tracked work plan and focused research workers.
- Steer an active run, stop it, or request an answer from the evidence already collected.
- Inspect model usage, worker progress, and the work plan through **Observe**; use **Focus** for reading.

Long conversations retain retrievable history and adapt to the selected model's context window.
An optional context-maintenance model can summarize older history.

### Paper Library

- Upload PDFs or acquire papers from a URL, then organize them into named collections.
- Read the original PDF alongside extracted text, paper notes, and saved summaries.
- Extract native PDF text and use Tesseract OCR for scanned pages, with optional vision-model OCR repair.
- Generate quick overviews or reviewed, citation-grounded summaries for one paper or a collection.
- Keep immutable summary versions and source-versioned evidence, with the ability to promote a saved version.
- Start a discussion from a paper, its summary, or its saved notes.

Each paper has a readable, stable folder containing its original PDF, notes, summaries, and evidence.

### Notes And Research Discovery

Create reusable knowledge notes and keep paper-specific notes beside their sources. Browse files,
organize them with tags, and edit Markdown directly in the workspace.

Research agents can find saved work through full-text search with ranked excerpts and tag filters.
Application edits update the index automatically; refresh the index after editing files externally.

![ScholarWeave notes workspace](docs/screenshots/workspace.png)

### Your Choice Of Models

Add provider profiles for Ollama, self-hosted OpenAI-compatible servers such as llama.cpp, or hosted
services such as OpenAI and OpenRouter. Configure the service's compatible base URL, API key where
required, and available models in **Settings**.

Choose models and supported reasoning settings for chat and paper summaries. ScholarWeave uses the
official OpenAI client with the Chat Completions API; provider support depends on compatible endpoints
and the model capabilities required for your task, such as tool calling or vision.

![ScholarWeave provider settings](docs/screenshots/settings-providers.png)

## Get Started

Requirements:

- Python 3.12+
- Node.js 20+ to build the frontend
- A configured model provider: Ollama, an OpenAI-compatible server, or a hosted service and API key
- Tesseract and language data for OCR of scanned PDFs

From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Set-Location frontend
npm.cmd install
npm.cmd run build
Set-Location ..

.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open [ScholarWeave](http://127.0.0.1:8000), add your provider in **Settings**, and select a chat model.
The backend serves both the API and the built frontend.

The app has no authentication, so keep its web server bound to `127.0.0.1`. This does not restrict
model connections: your provider can run on your machine, another server, or a hosted service.

**Upgrading an existing installation?** Follow the [workspace upgrade guide](docs/workspace-upgrade.md)
before starting with an older library layout. The offline upgrade backs up research files and the
database, reorganizes the library, and retires old conversations and agent runs.

## Data And Privacy

Provider profiles, settings, conversations, runs, and document metadata are stored in
`local_data/metadata.sqlite3`. Provider API keys are stored unencrypted on your machine; protect that
directory and its backups. Requests to a hosted provider send the prompts and research content needed
for the task to that service and are subject to its data policies.

| Path | Contents |
| --- | --- |
| `workspace/library/papers/<title--id>/` | Original PDF, paper notes, canonical summary, summary versions, and evidence |
| `workspace/knowledge/` | Reusable knowledge notes |
| `local_data/artifacts/` | Extracted text, manifests, and cached tool results |
| `local_data/run_logs/` | Credential-redacted operational run snapshots |

Paper folder identities remain stable when display names change. Collections organize papers through
metadata without moving their files. Both `local_data/` and `workspace/` are excluded from Git.

## Development

```powershell
# Backend tests
.\.venv\Scripts\python.exe -m pytest

# Frontend tests and production build
Set-Location frontend
npm.cmd test
npm.cmd run build

# Regenerate contracts after API schema changes
npm.cmd run generate:api
npm.cmd run check:api
```

Keep `openapi-typescript` pinned to **7.13.0**. See [AGENTS.md](AGENTS.md) for coding conventions,
[docs/architecture.md](docs/architecture.md) for the runtime and storage design, and
[docs/recipes.md](docs/recipes.md) for common development workflows.
