# ScholarWeave Architecture

ScholarWeave is a local-first research workspace for one researcher on one local machine. One
FastAPI process serves the API and built React application, runs a native agent harness, and stores
state in SQLite plus guarded local files.

> This document is the canonical architecture reference. See [recipes.md](./recipes.md) for change
> procedures.

## 1. System context

```mermaid
flowchart LR
    Researcher[Researcher] -->|Research chat<br/>Deep Work<br/>Library and runs| SPA[React SPA]

    subgraph Process["Single Uvicorn process"]
        API[FastAPI routers]
        Services[Feature services]
        Harness[Native agent harness]
        Tools[Application tool runtime]
        Broker[In-process event broker]

        API --> Services
        Services --> Harness
        Harness <--> Tools
        Harness --> Broker
        Broker --> API
    end

    SPA -->|HTTP commands and queries| API
    API -->|SSE run events| SPA

    Services <--> DB[(SQLite)]
    Tools <--> DB
    Tools <--> Files[(local_data and workspace)]
    Harness <--> Providers[OpenAI-compatible<br/>model providers]
    Tools <--> Sources[Web, arXiv, Wikipedia]
```

### Deployment boundary

- One researcher owns the whole library. There are no accounts, teams, sharing, permissions, or
  collaborative editing.
- One process and one Uvicorn worker.
- Bind to `127.0.0.1`; the application has no authentication.
- SQLite stores metadata and durable run history.
- `local_data/` stores generated extraction artifacts and operational run snapshots.
- `workspace/` stores source PDFs beside canonical paper notes/summaries, and reusable knowledge notes.
- Model traffic uses `POST /v1/chat/completions` through the official OpenAI client.

### Single-researcher product boundary

Every conversation, paper, folder, note, summary, and run implicitly belongs to the local
researcher. Domain records therefore do not carry user, tenant, membership, ownership, sharing, or
permission fields. Paper folders are personal organization labels, not shared containers.

Concurrency exists only to support the researcher's own work: a model may issue independent tool
calls, Deep Work may delegate a focused task, and background runs must survive interruption. Locks,
leases, and single-flight rules prevent the researcher's own tasks from colliding; they are not a
multi-user coordination layer. Do not add collaboration abstractions unless the product boundary
changes explicitly.

## 2. Process-scoped composition

All services are constructed **once per application process**, not once per request or chat.
`create_app()` stores the container on `app.state`; FastAPI dependencies retrieve that same
container for every request.

```mermaid
flowchart TD
    Start[Process imports app] --> CreateApp[create_app]
    CreateApp --> Compose[create_services]

    subgraph Container["ApplicationServices: one shared object graph"]
        Platform[Settings, SQLite sessions,<br/>SafeStorage, prompts]
        Provider[Provider runtime, client pool,<br/>model resolver, local inference gate]
        Research[Documents, OCR, retrieval,<br/>workspace, source search and download]
        Execution[Tool catalog and runtime,<br/>compiler, run service, event broker]
        Conversation[Conversation service,<br/>session factory, ConversationTurnService]
    end

    Compose --> Container
    Container --> AppState[app.state.services]
    AppState --> Browser[Researcher's browser requests]
    AppState --> Runs[Researcher's background runs]
```

Shared process-scoped objects are intentional:

| Component | Why it is shared |
|---|---|
| `ProviderClientPool` | Reuses provider clients and closes them once |
| `InferenceScheduler` | Protects local model/GPU capacity during the researcher's own concurrent work |
| `ConversationSessionFactory` | Prevents overlapping turns in the same conversation |
| `RunService` | Tracks tasks, leases, cancellation, steering, and recovery |
| `EventBroker` | Fans live events out to SSE subscribers |
| `ApplicationToolRuntime` | Gives every run the same application capabilities |

Startup performs only process-level initialization: settings/database setup, stale-ingestion
recovery, workspace-folder checks, run cleanup scheduling, and interrupted-run recovery. It does
not load every document or create a new service graph for a chat.

## 3. Product surfaces and workflows

The HTTP surface selects a product workflow; it does not dynamically route among arbitrary agents.

```mermaid
flowchart TD
    ResearchUI[Research Chat] -->|message + session capabilities| ResearchRoute[Conversation message route]
    ResearchRoute --> TurnService[ConversationTurnService]

    TurnService -->|standard turn| ResearchFlow[Research blueprint]
    TurnService -->|conversation promoted to Deep Work| DeepFlow[Deep Work blueprint]

    ResearchFlow --> Researcher[Researcher<br/>entry agent]
    DeepFlow --> Coordinator[Coordinator<br/>entry agent]
    Coordinator -->|focused_research_worker tool| Worker[Focused worker<br/>delegated agent]
```

`ConversationTurnService` owns the application use case:

1. Validate the conversation.
2. Detect the first turn.
3. Select the Research or Deep Work blueprint from the conversation's persistent capability.
4. Apply feature flags such as web access and fast-answer mode.
5. Compile the blueprint.
6. Attach the paper-work completion policy.
7. Touch the conversation and create a run.

It does not execute model turns. `RunService` supervises execution, and `AgentRunner` owns the
model/tool loop.

Deep Work enables execution but does not require it on every turn. The coordinator model judges
intent from the conversation, clarifies material ambiguity, and can answer discussion without a
plan. Once it creates a plan for requested research, pending/in-progress items enforce continuation
across epochs and recovery. There is no keyword classifier or missing-plan completion gate.

Response style is independent of this execution capability. The default `research` style follows
the requested outcome without requiring saved paper artifacts. `review` explicitly opts into a
reviewed summary and durable notes; `learn` and `understand` retain their narrower evidence checks.
The selected style is passed through Deep Work compilation and run metadata instead of being
coerced to review.

## 4. Blueprint compilation

Blueprints are provider-neutral, serializable workflow definitions. The exact blueprint is stored
with every run so recovery can compile it again without serializing live Python state.

```mermaid
flowchart LR
    Blueprint["AgentBlueprint<br/>agents, model refs, tools,<br/>delegation, budgets, session policy"]
    Validate[Validate IDs,<br/>references, topology]
    Resolve[Resolve provider profile<br/>and model capabilities]
    Bind[Bind catalog tools<br/>and delegation tools]
    Policy[Attach prompts,<br/>context and run policies]
    Compiled["CompiledAgent<br/>entry agent + definitions<br/>+ bindings + policies"]

    Blueprint --> Validate --> Resolve --> Bind --> Policy --> Compiled
```

The compiler produces:

- an `AgentDefinition` for each agent;
- a concrete `ModelBinding` with a pooled client and provider capabilities;
- JSON-schema-backed `FunctionTool` objects;
- explicit delegation tools;
- run limits and context-compaction policy;
- an optional deterministic completion validator.

## 5. Run lifecycle

```mermaid
sequenceDiagram
    actor User
    participant UI as React SPA
    participant API as Conversation router
    participant Turns as ConversationTurnService
    participant Runs as RunService
    participant DB as SQLite
    participant Runner as AgentRunner
    participant Model as Model provider
    participant Tools as ApplicationToolRuntime

    User->>UI: Send message
    UI->>API: POST conversation message
    API->>Turns: Start Research or Deep Work turn
    Turns->>Turns: Build and compile blueprint
    Turns->>Runs: Create run
    Runs->>DB: Persist input, blueprint, metadata
    Runs-->>API: Pending run
    API-->>UI: 202 Accepted

    Runs->>Runs: Claim lease and acquire conversation lock
    Runs->>DB: Begin epoch
    Runs->>Runner: Run durable history + new input

    loop Until final answer or budget boundary
        Runner->>Model: Stream Chat Completions request
        Model-->>Runner: Reasoning, text, tool-call fragments
        opt Tool calls returned
            Runner->>Tools: Execute validated tool calls
            Tools->>DB: Journal attempt and domain changes
            Tools-->>Runner: Bounded result or result_ref
        end
    end

    Runner-->>Runs: Result, generated items, usage
    Runs->>DB: Persist items, epoch, usage, final state
    Runs-->>UI: Events arrive over SSE
```

### Responsibility split

| Layer | Owns |
|---|---|
| Router | HTTP parsing and response shaping |
| `ConversationTurnService` | Selecting and launching one product workflow |
| `AgentCompiler` | Blueprint to executable agent definitions |
| `RunService` | Durable lifecycle, epochs, leases, recovery, cancellation, steering |
| `AgentRunner` | One bounded model/tool loop |
| `ApplicationToolRuntime` | Safe execution of ScholarWeave capabilities |

## 6. Native model/tool loop

The harness is a deterministic state machine around probabilistic model output.

```mermaid
stateDiagram-v2
    [*] --> PrepareContext
    PrepareContext --> CallModel
    CallModel --> StreamOutput
    StreamOutput --> FinalizeCalls

    FinalizeCalls --> ValidateFinal: No tool calls
    ValidateFinal --> Completed: Output is valid
    ValidateFinal --> Failed: Output or policy violation

    FinalizeCalls --> ExecuteTools: Tool calls present
    ExecuteTools --> AppendResults
    AppendResults --> PrepareContext: Continue work
    AppendResults --> BudgetExceeded: Explicit turn limit exhausted

    Completed --> [*]
    Failed --> [*]
    BudgetExceeded --> [*]
```

Canonical conversation items are plain provider-neutral dictionaries:

- user, assistant, system, and developer messages;
- function calls;
- function-call outputs;
- reasoning items.

They become Chat Completions messages only at the provider boundary. Streamed tool-call fragments
are reassembled by index, call ID, or last-seen call to tolerate OpenAI-compatible provider
differences.

The harness enforces:

- optional model-turn limits and input/output size limits;
- tool concurrency;
- per-turn single-flight tools;
- structured-output JSON Schema;
- maximum delegation depth;
- preservation of assistant text emitted alongside tool calls.

## 7. Tools and domain capabilities

```mermaid
flowchart TD
    Agent[AgentRunner] --> Tool[FunctionTool]
    Tool --> Runtime[ApplicationToolRuntime]

    Runtime --> Middleware["Shared execution policy<br/>cancellation, transport retries,<br/>attempt journal, exact results"]

    Middleware --> Sources[Web, arXiv,<br/>Wikipedia]
    Middleware --> Documents[PDF acquisition,<br/>OCR, reading, retrieval]
    Middleware --> Workspace[Notes, summaries,<br/>workspace search and writes]
    Middleware --> Plans[Deep Work plan state]
    Middleware --> Results[Large-result storage<br/>and targeted reads]

    Documents --> Storage[SafeStorage]
    Workspace --> Storage
    Documents --> DocDB[(Document metadata<br/>and chunks)]
    Plans --> RunDB[(Run goal state)]
```

The tool catalog is the model-visible contract: catalog ID, tool name, description, JSON schema,
strictness, and runtime handler. The runtime centralizes operational behavior so individual tools
do not each reinvent it.

### Tool result forms

```mermaid
flowchart LR
    Raw[Raw domain result] --> Bound{Fits context limit?}
    Bound -->|Yes| Inline[Inline model result]
    Bound -->|No| Stored[Stored large result]
    Stored --> Ref[result_ref receipt]
    Inline --> Context[Model context]
    Ref --> Context
    Raw --> Activity[Structured receipts<br/>and activity metadata]
    Activity --> Compaction[Context checkpoints]
    Activity --> Completion[Completion validation]
```

Safe reads may retry transient failures. Writes are not blindly retried; cancellation after dispatch
is journaled as an unknown outcome when success cannot be proven.

Tool failures have two representations. Operational records retain the original exception type and
message for diagnosis, while `tool.failed` events also carry a short, actionable
`display_message`. The activity UI renders only that readable message, so provider internals and
raw exception text are not presented as user guidance.

## 8. Delegated agents

Delegation is implemented as an explicit function tool, not an implicit handoff.

```mermaid
flowchart LR
    Parent[Coordinator] -->|self-contained request| DelegateTool[Delegation tool]
    DelegateTool --> Child[Focused worker AgentRunner]

    Shared["Shared<br/>run ID, tool runtime,<br/>metadata, receipts"]
    Isolated["Isolated<br/>conversation narrative<br/>and model stream"]

    Shared --- Child
    Child --- Isolated
    Child -->|compact final result| DelegateTool
    DelegateTool --> Parent
```

The worker cannot see the parent's transcript. Its lifecycle and usage remain visible, but its
token stream and assistant messages are filtered so they do not become the parent's answer.
Delegation is currently a nested, synchronous tool call rather than an independently scheduled
child run.

## 9. Events and frontend reconstruction

Events are the execution-to-UI contract, not merely logs.

The default research surface prioritizes the answer and editable composer. A session status strip
exposes cumulative token usage, weighted prefill/generation rates, and task progress. Observe opens
an overview of usage and workers, with the full trace behind a disclosure. Focus hides the chat list
and closes the panel while retaining the status strip and access to observability. Live reasoning
and per-turn performance use collapsed disclosures. Response style, execution capabilities, model
context/reasoning controls, and the fast-web shortcut sit under Options. Library
handoffs carry only a draft prompt identifying a paper or workspace path in `/?research=...`;
opening one starts no run and does not reopen an unrelated conversation. The shared Markdown
viewer memoizes unchanged content to avoid reparsing historical answers on each stream update.

```mermaid
flowchart LR
    Producers["Harness, hooks,<br/>tools, RunService"]
    Buffer["BufferedRunEventSink<br/>batch deltas and snapshots"]
    Persist["PersistedRunEventSink<br/>assign sequence"]
    EventDB[(agent_run_events)]
    Broker[EventBroker<br/>bounded subscriber queues]
    SSE["GET /runs/{id}/events"]
    UI[React stream and timeline]

    Producers --> Buffer --> Persist
    Persist --> EventDB
    Persist --> Broker
    EventDB -->|durable replay after cursor| SSE
    Broker -->|live tail| SSE
    SSE --> UI
```

Every event has a monotonically increasing sequence number. The browser de-duplicates by sequence.
On connection, the API subscribes to the broker, replays durable events, catches up from broker
history, and then tails live events. A lagging subscriber disconnects and can resume from its last
durable cursor.

The same event list reconstructs:

- streamed answer text and reasoning;
- tool calls, results, failures, and durations;
- delegated-agent lifecycle;
- context compaction;
- usage and run terminal state.

Each model attempt emits `model.telemetry` with a unique call ID. Overall usage includes main,
delegated, dedicated-summary, retry, and compaction calls; replay and forwarded child events
are deduplicated by call ID. Missing provider usage is marked incomplete rather than estimated
from output characters. Main-agent context is tracked separately: estimated request input before
generation, then provider-reported input plus output for the latest successful main-agent call.
Delegate and compaction measurements cannot replace that context indicator.

Prefill and generation speeds use llama.cpp response `timings` (`prompt_n`/`prompt_ms` and
`predicted_n`/`predicted_ms`), including timing-only terminal stream chunks. Each average is total
timed tokens divided by total server phase time, excluding queueing, tools, idle gaps, and network
latency. Missing timings display as unavailable; old wall-clock estimates are not relabeled as
server measurements. Visible speed readings update at most once every five seconds per run.
Session rates sum timed tokens and active durations across runs rather than averaging run rates.
Timed-call counters expose coverage; absent counters in older history are unknown, not zero.
Agent lifecycle and model/tool events carry invocation IDs so concurrent workers with the same name
remain distinct. Agent starts include a bounded assignment preview with an explicit truncation flag.
The frontend identifies the coordinator from lifecycle events, not the blueprint's display name.

Conversation-linked runs are excluded from automatic expiry because their events are the durable
session-usage ledger. Explicit history clearing, run deletion, and conversation deletion still
remove those records. Measurements from previously deleted history cannot be reconstructed.

## 10. Durability, epochs, and recovery

```mermaid
flowchart TD
    Run[Run record] --> Claim[Acquire renewable lease]
    Claim --> Lock[Acquire conversation run lock]
    Lock --> Epoch[Begin bounded epoch]
    Epoch --> Harness[Run AgentRunner]

    Harness -->|Final answer| Gate[Completion validator]
    Harness -->|Turn boundary or open plan| Save[Persist partial progress]
    Save --> Continue[Build continuation instruction]
    Continue --> Epoch

    Gate -->|Valid| Complete[Complete run]
    Gate -->|Invalid| Rollback[Rollback session checkpoint<br/>and fail run]

    Crash[Process interruption] --> Recover[Startup recovery]
    Recover --> Recompile[Recompile persisted blueprint]
    Recompile --> Claim
```

Durable run state is intentionally redundant:

```mermaid
erDiagram
    AGENT_RUNS ||--o{ AGENT_RUN_EVENTS : emits
    AGENT_RUNS ||--o{ AGENT_RUN_ITEMS : projects
    AGENT_RUNS ||--o{ AGENT_RUN_EPOCHS : contains
    AGENT_RUNS ||--o{ AGENT_TOOL_ATTEMPTS : journals
    AGENT_RUNS ||--o| AGENT_GOAL_STATES : tracks
    AGENT_RUNS ||--o| AGENT_RUN_CLAIMS : leases
    CONVERSATIONS ||--o{ SDK_SESSION_ITEMS : remembers
```

- **Run row:** status, input, blueprint, output, usage, runtime metadata.
- **Events:** durable chronological UI and diagnostic stream.
- **Items:** transcript-oriented projections.
- **Epochs:** bounded execution segments and continuation points.
- **Tool attempts:** retries, failures, result references, and uncertain writes.
- **Goal state:** Deep Work plan.
- **Claim:** current execution task and lease expiry for interruption recovery, not user ownership.
- **Session items:** canonical cross-turn conversation history.

Recovery starts a new epoch from durable facts. It does not attempt to deserialize a suspended
coroutine or resume a partially received model stream.

## 11. Context management

```mermaid
flowchart TD
    History[Canonical history] --> Normalize[Remove stale steering markers<br/>and superseded reads]
    Normalize --> Size{Above model-aware input<br/>high-water mark?}
    Size -->|No| Steering[Append queued steering]
    Size -->|Yes| Cache[Archive older re-readable output<br/>replace payloads with retrieval refs]
    Cache --> Fits{Enough space?}
    Fits -->|Yes| Steering
    Fits -->|No| Split[Split older and recent work]
    Split --> Preserve[Preserve current instructions<br/>and recent complete rounds]
    Split --> Archive[Cache exact older history]
    Archive --> Summarize[Summarize older history if enabled]
    Summarize --> Checkpoint[Structured checkpoint<br/>evidence, refs, receipts, plan]
    Preserve --> Rebuild[Checkpoint + constraints<br/>+ recent items]
    Checkpoint --> Rebuild
    Rebuild --> Steering
    Steering --> Model[Next model request]
```

Compaction occurs only between complete model/tool rounds. The compacted list becomes the runner's
new working context, while generated items remain available for durable history. A separate durable
session working snapshot and cursor allow subsequent turns to read the snapshot plus new items
instead of recompacting the original transcript. Snapshot recovery and rollback preserve
conversation ordering and steering.
Legacy lossy working snapshots are rebuilt from canonical session history on their next read;
versioned cache-backed snapshots then take over without deleting the original transcript.

The selected model's context window, minus response capacity, determines the input ceiling.
Legacy manual working-input, tool-output, and compaction-target settings no longer constrain
execution. Context preparation derives a proportional retention target instead of collapsing an
80k-token window to an 8k-token checkpoint.
Request accounting includes instructions and tool schemas and reports estimates through
`context.prepared` (the UI also accepts older `context.sized` events). Provider-reported prompt usage
calibrates subsequent per-agent estimates; these remain estimates, not exact provider tokenization.
Unfit irreducible instructions fail explicitly rather than being silently truncated. Fresh tool
outputs are not clipped to a fixed token allowance. Context preparation archives oversized evidence
when necessary; durable paper evidence makes superseded raw reads evictable.
Pending summary batches are resized to actual request space with matching source spans and
checkpoint cursors. Archived-only raw source cannot be claimed as complete read coverage.

Older cached tool payloads are evicted before narrative compression, preserving recent complete
model/tool rounds. Exact context history is retained behind paginated `read_tool_result` references;
conversation-scoped caches survive run-history cleanup and are deleted with their conversation.
Chat deletion first cancels and waits for its runs, then removes their database history, run-scoped
artifacts, operational snapshots (including snapshots retained after run cleanup), and conversation
caches. Saved library papers, notes, summaries, and workspace files remain independent research
outputs and are not deleted with a chat.
The shared provider traffic audit log is not conversation-scoped and is not erased by chat deletion.
Model-assisted summarization is a last resort for the older prefix. The deterministic-only setting
avoids its model call but retains excerpts rather than a semantic summary. Neither writing a cache
file nor provider KV caching reduces active context unless the request itself replaces or omits
the original text.
An optional `default_model_references.compaction` selects a helper through the normal provider
resolver. It uses its own known context window, not the coordinator's capacity or reasoning setting.
Insufficient capacity or an unusable helper response emits a compaction failure/fallback event and
retries the main model before using the existing explicit deterministic fallback. Provider model
switches use the shared inference scheduler.

Default chat, Deep Work, paper summaries, and delegated research have no total turn cap. The harness
represents unbounded work with `max_turns=None`, not a large sentinel. Epoch checkpoints remain bounded for
durability, without limiting the number of epochs. There is no total run wall-clock deadline.
Cancellation, network timeouts for stalled I/O, leases, and completion gates still apply.
There is no whole-tool wall-clock deadline around loading, prefill, execution, or queued mutations.
Explicit custom blueprint turn limits remain opt-in; stop-and-answer requests one response.

For automatic response allowances, `finish_reason=length` recomputes only the uncommitted model
turn with a doubled allowance, bounded by remaining context rather than a fixed output ceiling.
Every attempt contributes to usage. No partial tool calls enter execution or durable conversation
history. `model.retry` retracts partial assistant text in both live and persisted stream projections;
completed tool operations and prior assistant messages remain intact. Cancellation interrupts
recomputation normally. Explicit blueprint response caps are not silently overridden.
Recognized provider context-overflow rejections tighten calibration and rerun context preparation.
An identical rejected request is never resent; unrelated provider errors still surface explicitly.

## 12. Research data pipeline

```mermaid
flowchart LR
    Input[Uploaded or downloaded PDF] --> Source[Retain source PDF]
    Source --> Inspect[Inspect native text per page]
    Inspect --> Decision{Text adequate?}
    Decision -->|Yes| Extract[Extract text]
    Decision -->|No| OCR[Render and OCR selected pages]
    OCR --> Vision[Optional vision cleanup]
    Extract --> Format[Build Markdown and page manifest]
    Vision --> Format
    Format --> Chunks[Create cited searchable chunks]
    Chunks --> DB[(Documents, artifacts,<br/>chunks and metadata)]
    Format --> Files[(Document files)]
    DB --> Workspace[Ensure notes.md<br/>and summary.md]
```

PDF opening, lazy page-tree parsing, native page extraction, and Tesseract availability probes in
the asynchronous OCR paths are offloaded through AnyIO's existing worker pool. Pages are still
processed sequentially and callbacks remain on the event loop. This removes document-length event
loop stalls without multiplying page-image memory, model concurrency, or application workers.

The principal document entities are:

```mermaid
erDiagram
    PAPER_FOLDER o|--o{ DOCUMENT : groups
    DOCUMENT ||--o{ ARTIFACT : owns
    DOCUMENT ||--o{ DOCUMENT_CHUNK : contains

    DOCUMENT {
        string id
        string title
        string status
        int page_count
        json metadata
    }
    ARTIFACT {
        string kind
        string relative_path
        string sha256
        json metadata
    }
    DOCUMENT_CHUNK {
        int page_start
        int page_end
        string citation
        text text
        json embedding
    }
```

Folder assignment changes document metadata only; it does not move source PDFs or workspace files.
All user-controlled paths pass through `SafeStorage` or `WorkspaceService`.

`WorkspaceLayout` owns readable, bounded path components. `workspace_papers` stores each paper's
stable folder and display name separately from the derived search index. Papers live under
`library/papers/<title--id>/` with `source.pdf`, `notes.md`, `summary.md`, `summaries/` and `evidence/`.
New standalone notes live under `knowledge/<title--id>.md`. All paths are relative to the workspace
root; artifact resolution explicitly supports the workspace storage area and validates containment.
Ingestion resolves source artifacts through the repository instead of assuming a legacy PDF root.

The offline layout upgrade snapshots files and SQLite, verifies copies, updates metadata/index paths
transactionally, and then removes relocated originals. Its journal permits resuming an interrupted
upgrade. It retires old conversation/run rows without deleting research records. Server startup
refuses mixed layouts. Legacy JSON metadata is imported by this explicit upgrade, not by normal reads.
See [workspace-upgrade.md](workspace-upgrade.md) for restore behavior and operational requirements.

### Workspace discovery

| API (all under `/api`) | Purpose |
| --- | --- |
| `GET /documents` | Local paper library, including PDFs not yet prepared |
| `GET /workspace/notes` | Standalone Markdown notes and canonical paper notes |
| `GET /workspace/summaries` | Canonical summary files, including not-yet-filled templates |
| `GET /workspace/files` | Existing complete safe-file listing |
| `GET /workspace/search` | BM25-ranked text search or filtered browsing |
| `GET /workspace/index` | Index engine and indexed-file count, without a disk scan |
| `POST /workspace/index` | Reconcile external changes and atomically rebuild the index |

Notes, summaries and search accept `limit` (1-100, default 50) and zero-based `offset`. Search accepts
`query`, repeated `kinds`, and repeated `tags` (all supplied tags must match, case-insensitively).
Omit the query to browse. Query words are OR-matched and safely quoted, not interpreted as FTS
operators; punctuation-only queries return no matches. Results contain compact excerpts and positive
BM25 scores (higher is better), ordered by relevance, then modification time and path. Display titles
are weighted above content. This is lexical whole-token search, not embeddings or PDF source search.

The existing SQLite FTS5 table is the durable inverted index; no duplicate JSON index is needed.
Markdown, plain text, and JSON content plus file names, display titles, and tags are indexed.
Application writes/deletes update it in the same repository transaction as metadata. Refresh reads
safe files first, preserves existing names/IDs/tags, removes deleted paths, and replaces both tables
in one transaction. A process-local lock serializes refresh with workspace mutations; read failures
leave the previous index untouched. External editors are not watched: refresh explicitly after their
changes. Search/list calls query SQLite rather than reopening or scanning the workspace.

Agents share these services through `list_workspace` (bounded pages and `next_offset`),
`search_research_notes` (BM25 with pagination), and `workspace_index` (status/refresh). Canonical
summaries can be incomplete or stale: agents read selected artifacts and verify source citations.
Immutable summary history remains at `/documents/{id}/summaries`, separate from canonical discovery.

Paper content search uses SQLite FTS5 with transactional chunk-index triggers, not an embedding or
model call. Chunk reads use database `LIMIT`/`OFFSET`; page/chunk tool responses honor explicit
item/character continuation cursors without a hidden global output cap.

Summary jobs use one serial model worker. Short extractions fitting the context allowance are
included directly; longer papers use adaptive read/checkpoint batches with no fixed page cap.
Dedicated summaries reserve 10% of the configured effective model window for safety and 25% for
the initial response allowance; the remaining 65% covers input, including instructions and tool
schemas. An 80,000-token window therefore allows 52,000 input tokens, 20,000 output tokens, and
8,000 safety tokens. This allocation is summary-specific; general research retains its existing
context policy. Source admission measures the serialized request rather than imposing a fixed
character cap. Papers that do not fit continue through exact durable evidence cursors.
Reasoning defaults to off for both reviewed summaries and overviews when the model declares support
for disabling it. Explicit summary reasoning selections take precedence; unsupported selections
are rejected. Models without a supported off setting retain provider behavior.
Research agents use the dedicated summary tool to invoke an isolated persistent summary run instead
of drafting the summary in the general research transcript. Agent-requested summary jobs are
serialized over their entire lifetime. Their telemetry is forwarded to the parent without exposing
their transcript or replacing its main-agent context measurements.
Durable per-paper job receipts let recovered callers reattach to the same child run and replay
unforwarded telemetry instead of creating duplicate summaries.
The `paper_evidence` table retains source/extraction-versioned evidence independently of runs, with
a guarded `library/papers/<title--id>/evidence/<source-version>/index.json` workspace mirror. SQLite commits precede
mirror verification and raw-context eviction; interrupted mirrors are repaired from SQLite.
Records retain exact spans and model/prompt provenance. Only contiguous full coverage is marked
complete; otherwise saved summaries explicitly report partial coverage. Immutable summary versions
are replay-safe, and a late job does not replace a canonical summary changed since that job started.
Evidence never overwrites user notes; source changes invalidate generated summary/evidence reuse.

## 13. Deterministic safety and completion controls

```mermaid
flowchart LR
    ModelChoice[Model proposes work] --> Policies[Deterministic policies]
    Policies --> Turns[Opt-in custom blueprint turn budgets]
    Policies --> Cancellation[User cancellation]
    Policies --> Tools[Transport retries,<br/>concurrency, journaling]
    Policies --> Context[Context high-water mark]
    Policies --> Lease[Lease ownership]
    Policies --> Completion[Plan and paper-work gates]
    Completion --> Outcome{May run complete?}
```

A model saying “done” is not sufficient when a completion policy applies. Review mode requires
read/extraction, a complete cited summary, and durable notes. The default Research mode permits
discussion of saved work and screening acquired candidates without forcing those artifacts; actual
paper reads require a supplied citation in the answer. Learn/Understand modes permit targeted paper
Q&A without full-review side effects, but enforce citations against observed paper reads.
Deep Work must also close or block every tracked work item. Artifact intent outside explicit review
is model-guided, not a keyword classifier or a hard write-permission gate.

Tool retry policy is action-aware: paper preparation and summary coverage advancement are writes,
even when exposed under a read tool. Pure reads use bounded retry deadlines with Retry-After and
jitter. Unknown write outcomes are reconciled, not blindly retried. Source-specific repeated
failures pause that source rather than disabling access to unrelated papers or URLs.

All model requests share one application-wide inference lane, even for the same model or different
provider profiles. The transport holds it until the response is consumed or closed, not across tool
execution or delegation, so child requests and compaction do not deadlock behind a coordinator.
Interactive requests take priority between responses; a waiting background request is admitted
after three interactive calls to prevent starvation. Queued cancellation does not interrupt an active
response. The legacy `serialize_model_switches` field remains accepted for stored-profile/API
compatibility, but cannot bypass the global lane and is no longer exposed as a concurrency toggle.
The server owns model loading and hot swaps; `/v1/models` may list any number of models.
There is no manual load confirmation or restart pause. Old residency configuration is ignored,
and provider updates remove it without a database cutover or deletion of research history.

## 14. Extension map

| Change | Primary extension point |
|---|---|
| Add an HTTP endpoint | Feature router, schema, service, generated OpenAPI client |
| Add a model-callable tool | Tool catalog, runtime handler, model-visible prompt guidance |
| Change Research or Deep Work composition | Blueprint builders in `conversations/turns.py` |
| Add a provider kind | Provider schemas/runtime/model resolver |
| Change model-loop behavior | Native harness |
| Change run durability or recovery | Run service, repository, and run models |
| Add an event type | Backend producer and frontend `RUN_EVENT_TYPES` |
| Change context compaction | Context budget policy |
| Change paper ingestion | Document ingestion/OCR/vision pipeline |
| Add a user-visible research artifact | Domain service plus `SafeStorage` or `WorkspaceService` |

## 15. Architectural invariants

1. Routers call services; only repositories open ORM sessions.
2. All research data implicitly belongs to one local researcher; do not add collaboration or
   tenancy layers.
3. The application uses one process-scoped service graph.
4. The native harness owns the model/tool loop; domain behavior stays behind tools.
5. Canonical conversation items remain provider-neutral.
6. Persist events before publishing them.
7. Treat SQLite and guarded files as the durable recovery boundary.
8. Never write user-facing paths outside `SafeStorage` or `WorkspaceService`.
9. Do not run multiple Uvicorn workers without replacing the in-memory control plane.
10. Keep event names synchronized with the frontend subscriber.
11. Let deterministic completion policies—not model confidence—decide whether constrained work is
    complete.
