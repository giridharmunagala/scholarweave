# ScholarWeave

ScholarWeave is a local-first assistant for conversation, exploring ideas, working with documents,
and looking things up on the web. Start with your question; use papers and notes as sources when
relevant, and save summaries or other outputs only when you need them.

Connect Ollama, your own OpenAI-compatible server, or a hosted service with your own API key.
Your library is stored on your machine; model inference runs with the provider you choose.

## Features

### Chat, Local Sources, And Web Lookup

- Search the web, arXiv, and Wikipedia; acquire PDF papers and web sources.
- Ask questions about papers, compare findings, and follow citations back to the evidence.
- Attach Markdown, plain-text, or PDF files in chat and request a summary, document Q&A, or supported research tasks from the file.
- Chat, brainstorm, draft text, or ask for an explanation without a mandatory research workflow.
- Choose per-message effort: **Auto**, **Quick**, or **Thorough**. Quick can use local files and
  attachments as well as the web; Thorough enables focused workers without permanently changing the chat.
- Ordinary conversation and targeted lookups need no plan. Longer assignments can use a tracked plan.
- Summaries appear in chat unless you request a saved artifact. Discussing a saved summary does not regenerate it.
- Steer an active run, stop it, or request an answer from the evidence already collected.
- **Observe** shows the full trace, with Deep Work workers nested beneath their delegation calls.
  Usage and work-plan details stay collapsed; use **Focus** for reading.
- A compact activity line stays above the composer with elapsed time and recent trace headlines.
  Usage also shows prompt-cache reuse when the provider reports it; unsupported providers need no
  extra configuration.

Long conversations retain retrievable history and use the **Context** size selected in chat Options.
That size controls compaction, including an optional context-maintenance model, and overrides provider
context metadata whether smaller or larger. Reopening a chat restores the size used for its latest
run. The model server must actually support the size you select.

Use **Attach file** beside the chat composer, then describe the task and send your message. Uploads
are copied to the local workspace and indexed immediately, without a server restart or model-driven
copy step. PDFs use the existing native extraction/OCR pipeline and remain in the paper library;
their extracted text is also searchable in the workspace. Re-uploading identical files reuses existing
work where possible. Removing an attachment from a draft does not delete its saved research file.

For example: "Summarize all research papers in this proposal's references." ScholarWeave reads the
proposal, looks for existing local evidence, and reports unavailable or unresolved references rather
than inventing summaries. It asks when the intended task is ambiguous and declines unsupported work
such as building or executing software. Uploading a file alone does not start a task. Attachments are
available at every response effort, but not in active-run steering. Effort returns to Auto after sending;
web access is controlled separately. Code execution, computational data analysis, and computer control
are not supported.

New conversations get a title from the first message locally, without an extra model call. Existing
Deep Work conversations can use any effort through the same chat interface. Response effort controls
the requested scope, not a fixed latency guarantee; model speed, context size, and source retrieval
still affect response time.

The default limits are 10 attachments per message, 2 MiB per UTF-8 Markdown/text file, and 40 MiB per
PDF. A PDF's extracted workspace text must also fit the 2 MiB text limit; oversized extraction returns
an error while preserving the original in the library. Scanned pages require Tesseract and its
language data; encrypted PDFs must be unlocked before upload.

### Paper Library

![ScholarWeave paper library](docs/screenshots/papers.png)

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

### Terminal Research Cockpit

Prefer the terminal? The optional Textual interface brings a themed, keyboard-first
cockpit to the same local library: streaming conversations, searchable papers with extracted text
and citations, editable notes, and a live observatory for tools, usage, and work plans.

Install the terminal extra, then launch:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[tui]"
.\.venv\Scripts\python.exe -m scholarweave_tui
# Or, with the virtual environment activated:
scholarweave
```

Use `--api-url http://127.0.0.1:8001` if your backend uses another port. The client accepts only
loopback HTTP servers. The launcher reuses a healthy backend for this library, or starts one on
`127.0.0.1` and waits until it is ready. It refuses to connect to an unrelated service or another
library. **Quitting stops only a backend this TUI started**; a pre-existing server is left running.
Use `--connect-only` to require an existing server without starting one. The TUI itself never opens
the database or writes research files directly. Configure your default chat model in the web app's
**Settings** first; provider
setup, PDF imports, extraction, collection management, and native PDF viewing remain in the web app.
The normal terminal command does not require Node.js or a frontend build. Add `--build-frontend`
to build the web app before startup, or `--check` to check startup and exit without opening the TUI.

**One-click Windows shortcut:** from the repository root, run this small installer once:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows-launcher.ps1
```

It creates **ScholarWeave** shortcuts on your desktop and in Start. Right-click the shortcut
(**Show more options** on Windows 11), or find it in Start, and choose **Pin to taskbar**.
The shortcut builds the frontend, starts or reuses the backend, and opens the TUI. There is no
custom executable, runtime bundle, admin install, or global execution-policy change.

Keep this checkout and its `.venv` in place; the shortcut points to them. Node.js 20+ and the
frontend dependencies must already be installed (`npm ci` from `frontend` if needed). Build or
startup errors stay visible in the launcher window. Closing the TUI also stops research on a
backend it owns, so finish important work first; another TUI or browser using that same owned
server loses its connection too. A reused server is never stopped. An existing server started
without frontend assets may need a manual restart before it can serve the newly built web app.

| Key | Action |
| --- | --- |
| `/` in the composer | Slash commands: model, reasoning, effort, web, focus, theme, status |
| `Ctrl+M` | Choose the model for new conversations |
| `Ctrl+G` | Choose the reasoning level for the next message |
| `Ctrl+F` | Focus mode: hide or restore the side rails |
| `Ctrl+1` / `Ctrl+2` / `Ctrl+3` | Chat / Papers / Notes |
| `Ctrl+K` | Find conversations or papers; full-text search notes |
| `Ctrl+N` | New conversation or note |
| `Enter` in the composer | Send; while running, queue steering |
| `Shift+Enter` / `Ctrl+J` | New line |
| `Esc` | Stop the active run |
| `Ctrl+S` | Save the current note through the workspace API |
| `Ctrl+E` | Switch a note between preview and editor |
| `Ctrl+O` / `Ctrl+B` | Toggle the live observatory / library sidebar |
| `Ctrl+R` | Refresh lists and reconnect the selected conversation |
| `Ctrl+T` | Change theme |
| `Ctrl+P` | Command palette |
| `F1` | Keyboard shortcuts and the full command list |
| `Ctrl+Q` | Quit; stop the backend only if this TUI started it |

The cockpit always opens to **a fresh chat**, never the previous conversation. Existing threads
remain in the library and reopen only when you select one. A new conversation is saved when you
send its first message, so simply launching the app does not leave empty threads behind.
`Ctrl+F` toggles focus mode, and your layout choice is remembered independently of the open chat.

Transcript messages stay centered independently: longer questions and answers expand from a
120-column reading width up to 160 columns without moving earlier messages or activity panels.
Both widths shrink to fit smaller terminals, and the composer stays centered as answers stream.

Type `/` in the composer to set up the next message without leaving it. `Tab` completes a command,
`Enter` runs it, `Esc` leaves your text alone. `/model` and `/reasoning` open pickers; `/reasoning`
offers only the levels a model declares, and remembers the level per model. Model choice is saved as
your chat model in backend settings, so a new thread starts on it. `/effort auto | quick | thorough`
and `/web on | off` set the next message, `/status` reports what it will use, and the status line
under the composer always shows the current setup.

Six themes ship with the cockpit: **Midnight Weave** (default), **Parchment**, **Aurora**, **Verdant**,
**Ember**, and **Glacier**. `Ctrl+T` previews them live and remembers your choice in
`local_data/tui-preferences.json`; only presentation and composer setup are stored there, never
research behaviour.

Mouse navigation works too. A wide terminal (roughly 140 columns) shows all three panes once focus
mode is off; smaller terminals collapse the side panels (minimum 60 columns by 24 rows). While a run
is active, a line above the composer shows the phase, the elapsed clock, and a **Stop** button; it
disappears when the run settles. Effort is per message and resets to Auto after sending.
Switching threads does not cancel backend work; reopening a thread restores its history
and resumes live events. Connection errors preserve the composer draft and offer refresh rather
than silently retrying a write.

Notes have a Markdown preview and editor. Unsaved changes are guarded when opening another note or
quitting, and saving checks for external edits before writing. That check is best-effort, not an
atomic compare-and-swap: avoid editing the same note simultaneously in another client. Note search
uses the existing workspace index; refresh that index in the web app after external file edits.

## Data And Privacy

Provider profiles, settings, conversations, runs, and document metadata are stored in
`local_data/metadata.sqlite3`. Provider API keys are stored unencrypted on your machine; protect that
directory and its backups. Requests to a hosted provider send the prompts and research content needed
for the task to that service and are subject to its data policies.

| Path | Contents |
| --- | --- |
| `workspace/library/papers/<title--id>/` | Original PDF, paper notes, canonical summary, summary versions, and evidence |
| `workspace/knowledge/` | Reusable knowledge notes |
| `workspace/inbox/attachments/` | Chat-uploaded Markdown and plain-text files |
| `local_data/artifacts/` | Extracted text, manifests, and cached tool results |
| `local_data/run_logs/` | Credential-redacted operational run snapshots |

Paper folder identities remain stable when display names change. Collections organize papers through
metadata without moving their files. Both `local_data/` and `workspace/` are excluded from Git.

## Development

```powershell
# Backend tests
.\.venv\Scripts\python.exe -m pytest

# Optional terminal UI tests
.\.venv\Scripts\python.exe -m pip install -e ".[dev,tui]"
.\.venv\Scripts\python.exe -m pytest backend\tests\test_tui_app.py backend\tests\test_tui_client.py backend\tests\test_tui_stream.py backend\tests\test_tui_integration.py backend\tests\test_tui_launcher.py backend\tests\test_architecture.py

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
