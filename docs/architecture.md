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
- `local_data/` stores source documents, generated artifacts, and operational run snapshots.
- `workspace/` stores user-visible Markdown research files.
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
    ResearchUI[Research Chat] --> ResearchRoute[Research message route]
    DeepUI[Deep Work] --> DeepRoute[Deep Work message route]

    ResearchRoute --> TurnService[ConversationTurnService]
    DeepRoute --> TurnService

    TurnService --> ResearchFlow[Research blueprint]
    TurnService --> DeepFlow[Deep Work blueprint]

    ResearchFlow --> Researcher[Researcher<br/>entry agent]
    DeepFlow --> Coordinator[Coordinator<br/>entry agent]
    Coordinator -->|focused_research_worker tool| Worker[Focused worker<br/>delegated agent]
```

`ConversationTurnService` owns the application use case:

1. Validate the conversation kind.
2. Detect the first turn.
3. Build the Research or Deep Work blueprint.
4. Apply feature flags such as web access and fast-answer mode.
5. Compile the blueprint.
6. Attach the paper-work completion policy.
7. Touch the conversation and create a run.

It does not execute model turns. `RunService` supervises execution, and `AgentRunner` owns the
model/tool loop.

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
    AppendResults --> PrepareContext: Turns remain
    AppendResults --> BudgetExceeded: Turn budget exhausted

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

- model-turn and input/output size limits;
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

    Runtime --> Middleware["Shared execution policy<br/>timeout, cancellation, retries,<br/>attempt journal, result bounds"]

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
    Normalize --> Bound[Bound oversized tool outputs]
    Bound --> Size{Above context<br/>high-water mark?}
    Size -->|No| Steering[Append queued steering]
    Size -->|Yes| Split[Split older and recent work]
    Split --> Preserve[Preserve every user message verbatim]
    Split --> Summarize[Summarize discarded model/tool history]
    Summarize --> Checkpoint[Structured checkpoint<br/>evidence, refs, receipts, plan]
    Preserve --> Rebuild[Checkpoint + user messages<br/>+ recent items]
    Checkpoint --> Rebuild
    Rebuild --> Steering
    Steering --> Model[Next model request]
```

Compaction occurs only between complete model/tool rounds. The compacted list becomes the runner's
new working context, while generated items remain available for durable history.

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

## 13. Deterministic safety and completion controls

```mermaid
flowchart LR
    ModelChoice[Model proposes work] --> Policies[Deterministic policies]
    Policies --> Turns[Turn and epoch budgets]
    Policies --> Deadline[Wall-clock deadline]
    Policies --> Tools[Tool timeout, retries,<br/>concurrency, journaling]
    Policies --> Context[Context high-water mark]
    Policies --> Lease[Lease ownership]
    Policies --> Completion[Plan and paper-work gates]
    Completion --> Outcome{May run complete?}
```

A model saying “done” is not sufficient when a completion policy applies. Paper-based work must
show the required read/extraction, cited summary, and durable notes activity. Deep Work must also
close or block every tracked work item.

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
