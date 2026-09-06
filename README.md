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

Choose a response style under **Options** in the chat composer:

- **Follow my request** (default): discuss, explain, compare, or investigate without automatically
  creating saved summaries or notes. Ask explicitly when you want a durable artifact.
- **Learn / ask**: explain concepts using the smallest sufficient evidence set.
- **Explain in depth**: answer targeted paper questions with citations, without requiring a full summary.
- **Review + save**: explicitly request the full paper summary/notes completion checks.

These choices apply per turn, including in Deep Work. Deep Work enables unattended execution, not
an automatic requirement to summarize every paper. The composer remains available during a run:
send a direction or constraint for the next model call, stop, or request an answer from evidence
already collected.

The library's **Discuss paper**, **Analyze summary**, and **Analyze saved work** actions open
editable drafts in a new chat. They do not send a message or start generation. Suggestions are
editable drafts too. **Options** holds response style, web access, Deep Work, context, reasoning,
and the fast-web shortcut, leaving the composer focused on writing.

The session status strip keeps total token consumption, average prefill/generation speeds, and
worker progress visible above the conversation. **Observe** opens usage details, worker assignments,
the work plan, and a collapsible full trace without leaving the chat. Live reasoning and per-turn
**Performance** remain optional disclosures. **Focus** hides the chat list and closes observability;
the status strip and one-click access to details remain available. Use **Alt+Shift+F** for Focus,
**Alt+Shift+A** for Observe, and **Escape** to dismiss options or observability without losing a draft.

Deep Work keeps an explicit plan when research execution is requested. For a collection, keep an index, cited evidence,
comparisons, and open questions in research notes rather than repeatedly loading every paper into
the conversation.

### Deep Work

Deep Work uses the same research tools plus three tracker operations:

- `create_work_plan`
- `read_work_plan`
- `update_work_item`

The model first understands intent: discussion, brainstorming, and clarification do not force a plan.
Once the user requests a sufficiently scoped research task, it creates the plan. No keyword triggers
decide this. The run loop feeds open items back to the model and continues across durable checkpoint epochs.
There is no default total turn, epoch, or elapsed-time ceiling. Completion still requires the work
plan to be settled; cancellation and genuine provider/tool errors remain visible. A focused worker
remains available for genuinely independent research tracks.

### Find existing research

Agents can browse papers, notes, summaries, and files with `list_workspace`, then use
`search_research_notes` for BM25-ranked lexical search with excerpts and pagination.

HTTP discovery is available at:

- `GET /api/documents` for papers;
- `GET /api/workspace/notes` and `GET /api/workspace/summaries` for saved artifacts;
- `GET /api/workspace/search?query=paged%20attention&limit=10&offset=0` for workspace text.

Search supports kind and tag filters and matches any query word; omit `query` to browse. It uses the
existing persistent SQLite FTS5 inverted index, not an LLM or a second JSON index. Application writes
keep it current. After editing files outside ScholarWeave, call `POST /api/workspace/index` or ask the
agent to refresh with `workspace_index`. `GET /api/workspace/index` reports indexed file count.
Workspace search does not search PDF source bodies; paper retrieval remains separate.

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
Deleting a folder returns its papers to the default Papers group. Deleting a paper removes its
managed PDF, extracted artifacts, summaries, and notes.

Paper summaries offer a quick, explicitly partial overview and a coverage-aware reviewed summary.
Quick overviews disable reasoning when the selected model is known to support `none`, unless an
explicit effort is requested. Reviewed summaries and ordinary chat retain their reasoning defaults.
**Summarize a collection** queues selected prepared papers on one explicitly selected model.
Generated evidence is keyed to the source/extraction version and survives run-history cleanup.
User-authored notes remain separate.

### One GPU and a hot-swappable llama server

Keep the main model resident for interactive work and occasional summaries. Switching to a smaller
model is useful only when an entire batch saves more than both model switches and any extra review.
For example, two 20-second switches add approximately 40 seconds before any net benefit.

ScholarWeave uses one application-wide inference lane, even across different provider profiles.
Interactive requests get priority between model responses; a waiting background request is admitted
after three interactive calls to avoid starving unattended work. The provider's **One model at a
time** setting does not increase this global capacity. A currently generating response is not
preempted by a new chat request.

Your server manages loading and hot swaps based on the requested model. ScholarWeave never silently
substitutes a smaller model. `/v1/models` can advertise multiple available models; no manual
confirmation is needed, including after restarting the application. Old residency protection
settings no longer pause requests.

Native PDF parsing/extraction and Tesseract availability checks run in the existing worker pool so
they do not hold the API event loop for an entire document. Pages remain sequential to bound memory;
this improves responsiveness, not GPU throughput. Unchanged Markdown answers are memoized so
typing and streaming another turn do not repeatedly parse their tables, math, and citations.

For a CPU-rich, VRAM-limited machine, keep one main model resident and use the server's actual
per-request context capacity. Larger contexts consume more KV-cache memory and prompt-processing
time; do not raise them simply because system RAM or CPU core count is high. No extra model
parallelism, Uvicorn workers, or hardware-specific thread counts are enabled automatically.

### Long conversations and context

Input capacity follows the selected model's configured context window, with room for generation.
There are no fixed working-input or tool-output caps. A proportional retention target avoids
shrinking a large window to a tiny checkpoint. Legacy manual budget settings no longer control
execution and are not offered in the runtime settings UI.

Under context pressure, older re-readable tool output is moved out of the request first, while
recent exchanges stay intact. Exact archived history can be retrieved in bounded slices with
`read_tool_result`; summaries are a last resort for older history, not a replacement for the
latest interaction. Conversation caches survive run-history cleanup and are removed with the
conversation. This extends recoverable memory, not the model's physical context window.

Working memory is separate from the full transcript and is reused across turns. Model-assisted
summarization remains optional; disabling it retains cached history and deterministic excerpts
rather than a semantic summary.

In **Settings > Models > Context maintenance**, optionally select a smaller local model such as
`gemma4-12b`. It summarizes older history using its own context capacity and independent reasoning
settings; unavailable, insufficient-capacity, or failed helpers fall back visibly to the main model.
Leaving it unset uses the main model directly. A single application-wide inference lane
serializes all model calls, including chat, summaries, and compaction, across every provider.
Queued chat calls get priority between responses, with a background turn after three interactive
admissions when background work is waiting. The server handles hot swaps without manual confirmation.
Configure the effective per-request
context, not the training window or total context shared across server slots.

Dedicated paper summaries default to reasoning off when supported, with an explicit reasoning
selector for single-paper and collection jobs. Their context allowance reserves 10% for safety,
25% for output, and 65% for input including prompt/tool overhead. Large papers continue through
durable evidence checkpoints; research-requested summaries use the same isolated serial writer.
The model-callable summary tool accepts only the paper and summary mode: provider, model, and
reasoning are runtime-owned, not generated tool arguments. Chat reasoning choices are remembered
per provider/model, filtered against that model's declared capabilities, and never carried into the
summary writer. Explicit reasoning overrides in the paper-summary UI/API remain available.

Chat displays overall model token consumption separately from the main agent's current context.
Prefill and generation speeds are server active-time weighted averages, refreshed at most every
five seconds. They exclude idle/queue/tool time and show as unavailable when the server does not
provide timing measurements.
Conversation-linked runs are preserved by automatic retention so session totals do not silently
shrink over time. Explicitly clearing/deleting history still removes those measurements; previously
deleted usage cannot be recovered. Unreported tokens and partial server-timing coverage are labeled.

The response allowance includes a reasoning model's thinking tokens. If generation reaches it,
the harness automatically recomputes that unfinished turn with a larger allowance, up to the
remaining model context. Previously completed tools are not repeated, and incomplete tool calls
never execute. The display retracts the partial answer and shows **Recomputing response**.
There is no separate automatic output-token ceiling. Explicit blueprint response limits remain
explicit limits; a provider response that cannot complete within the available context fails
honestly rather than being presented as finished research.
Provider-reported token usage calibrates subsequent estimates. Recognized context-overflow
rejections trigger tighter context preparation rather than replaying completed research.

Chat, Deep Work, paper summaries, and delegated research have no default total turn or run-time cap.
Epochs are durability/checkpoint intervals, not conversation limits. Network timeouts still detect
stalled I/O; there is no whole-tool elapsed-time ceiling.
Stop-and-answer intentionally requests just one answer.
Explicitly budgeted custom blueprints can still request a finite turn limit.

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

To inspect model-call latency without printing prompt or response contents:

```bash
.venv/bin/python scripts/report_model_performance.py local_data/llm_calls.jsonl
```

The report includes call counts, token usage, queue time, request duration, and first-body-byte
latency where recorded. First body bytes may be SSE metadata or heartbeats, not generated tokens.
Missing measurements are reported as unknown. Compare identical tasks and cache conditions before
claiming a speedup; a smaller model or an additional worker is not automatically faster.

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
