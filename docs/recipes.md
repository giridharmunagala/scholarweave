# Recipes

Step-by-step procedures for the changes that come up repeatedly. Each recipe lists **every file you
must touch** — follow it and you should not need to explore the repo.

Read [`AGENTS.md`](../AGENTS.md) first for the golden rules; see
[`architecture.md`](architecture.md) when a recipe references a subsystem you don't know.

- [1. Add an API endpoint](#1-add-an-api-endpoint)
- [2. Add a new agent-callable tool](#2-add-a-new-agent-callable-tool)
- [3. Add a database table / column](#3-add-a-database-table--column)
- [4. Add a frontend page](#4-add-a-frontend-page)
- [5. Add a run event type](#5-add-a-run-event-type)
- [6. Add a configuration setting](#6-add-a-configuration-setting)
- [7. Add a whole new backend feature package](#7-add-a-whole-new-backend-feature-package)
- [8. Write a test without calling a real model](#8-write-a-test-without-calling-a-real-model)
- [9. Change an agent's prompt or default behaviour](#9-change-an-agents-prompt-or-default-behaviour)
- [10. Debug a failing agent run](#10-debug-a-failing-agent-run)

---

## 1. Add an API endpoint

**Files:** `backend/<feature>/schemas.py`, `service.py`, `repository.py` (if it touches the DB),
`router.py`, then regenerate contracts.

1. **Schema** — add Pydantic request/response models in `backend/<feature>/schemas.py`. Response
   models are what the frontend gets typed against, so name them `<Thing>Response`.

2. **Repository** (only if new data access is needed) — add a method that opens its own session:

   ```python
   def get(self, path: str) -> WorkspaceEntry | None:
       with self._session_factory() as session:
           return session.get(WorkspaceEntry, path)
   ```

   Raise `NotFoundError` from `backend.core.errors` for missing rows.

3. **Service** — add the business logic. It may call other services, but must not import `fastapi`.
   Raise `NotFoundError` / `ConflictError` / `ValidationError`; the global handlers map them.

4. **Router** — thin. Get the container via `Depends(services)`, call the service, build the
   response model:

   ```python
   @router.get("/content", response_model=WorkspaceFileContentResponse)
   def read_file(
       path: str = Query(min_length=1),
       container=Depends(services),
   ) -> WorkspaceFileContentResponse:
       document = container.workspace.read_file(path)
       return WorkspaceFileContentResponse(path=document.path, ...)
   ```

   Use `status_code=status.HTTP_204_NO_CONTENT` and return `Response(status_code=204)` for deletes.
   If this is a brand-new router module, also register it in the tuple in `backend/app.py`.

5. **Regenerate the contract** — required, non-optional:

   ```bash
   cd frontend && npm run generate:api
   ```

   Commit `frontend/openapi.json` and `frontend/src/api/schema.generated.ts`.

6. **Frontend** — add the call to the feature's `api.ts` using the generated type
   (see [recipe 4](#4-add-a-frontend-page)).

7. **Verify:** `cd frontend && npm run check:api` and the feature's backend test file.

---

## 2. Add a new agent-callable tool

This is the most common request and has **exactly two required edit sites**. Nothing statically
enforces that they stay in sync — a `catalog_id` declared without a handler only fails at runtime,
when a model calls it, with `ValueError: Unknown application tool '<id>'`. Check it yourself
(step 4).

### Step 1 — declare it in the catalog

`backend/tools/catalog.py`, append to `APPLICATION_TOOLS`:

```python
(
    "workspace.outline.build",          # catalog_id: dotted, <area>.<verb>, internal key
    "build_workspace_outline",          # name: snake_case, what the model sees
    "Build a heading outline for one Markdown file in the workspace.",   # shown to the model
    _object_schema(
        {
            "path": {"type": "string", "minLength": 1},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 6},
        },
        required=["path", "max_depth"],
    ),
    True,                               # strict_json_schema
),
```

Notes:
- `_object_schema()` already sets `additionalProperties: False`.
- With `strict=True`, **every property must be listed in `required`**. To make an argument optional
  under strict mode, give it a nullable type (`{"type": ["string", "null"]}`) and still require it —
  this is the existing convention (see `tools.search`).
- The description is prompt surface. Say when to use the tool, not just what it does.

### Step 2 — implement and register the handler

`backend/tools/runtime.py`. Add an entry to the `handlers` dict in `ApplicationToolRuntime.invoke()`:

```python
handlers = {
    ...
    "workspace.outline.build": self._build_workspace_outline,
}
```

Then add the method. Signature is always `(arguments, context)`, sync or async, returning something
JSON-serializable:

```python
def _build_workspace_outline(
    self,
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    document = self._workspace.read_file(str(arguments["path"]))
    max_depth = int(arguments["max_depth"])
    headings = [
        {"level": len(match.group(1)), "title": match.group(2).strip()}
        for match in re.finditer(r"^(#{1,6})\s+(.*)$", document.content, re.MULTILINE)
        if len(match.group(1)) <= max_depth
    ]
    return {"path": document.path, "headings": headings}
```

You do **not** need to handle: result truncation, safety limits, progress marking, or the
`tool.application_completed` event — `invoke()` does all of that around your handler.
Collaborators available on `self`: `_settings`, `_documents`, `_retrieval`, `_storage`,
`_workspace`, `_research_search`, `_source_downloads`, `_agents`, `_function_tools`, `_catalog`,
`_direct_agents`, `_conversation_memory`.

### Step 3 — make sure an agent can use it

A tool in the catalog but in no blueprint is discoverable via `search_available_tools` but not
bound to any agent. Add its `catalog_id` where it belongs:

| Agent | Add the `catalog_id` to |
| --- | --- |
| Main research / chat agent | `AUTONOMOUS_TOOL_IDS` in `backend/autonomous/service.py` (line ~17) |
| Extended-work coordinator | `EXTENDED_WORK_TOOL_IDS`, same file (line ~57) |
| Extended-work focused worker | `FOCUSED_WORKER_TOOL_IDS`, same file (line ~66) |
| Agent builder | the blueprint assembled in `backend/builder/service.py` |
| Fixed research agents | `backend/direct_agents/service.py` |
| Saved/template agents | the `tools` + `tool_ids` arrays in `backend/agents/templates.py` |

`AUTONOMOUS_TOOL_IDS` and `EXTENDED_WORK_TOOL_IDS` are `(tool_id, catalog_id)` pairs — `tool_id` is
the blueprint-local id you choose (kebab-case), `catalog_id` is the key you added in step 1.
`FOCUSED_WORKER_TOOL_IDS` is a flat tuple of *`tool_id`s only*, selecting a subset of tools already
declared in `AUTONOMOUS_TOOL_IDS`.

### Step 4 — verify

Confirm every declared tool has a handler and vice versa (no test does this for you):

```bash
.venv/bin/python - <<'PY'
import inspect, re
from backend.tools.catalog import APPLICATION_TOOLS
from backend.tools.runtime import ApplicationToolRuntime

declared = {cid for cid, *_ in APPLICATION_TOOLS}
handled = set(re.findall(r'"([\w.]+)":\s*(?:self\._|\w+[,\n])',
                         inspect.getsource(ApplicationToolRuntime.invoke)))
print(f"declared={len(declared)} handled={len(handled)}")
print("declared without a handler:", sorted(declared - handled) or "none")
print("handlers with no catalog entry:", sorted(handled - declared) or "none")
PY
```

Both lists must print `none`.

Then run the tool tests:

```bash
.venv/bin/pytest backend/tests/test_sdk_tools.py
```

`test_sdk_tools.py` checks that catalog entries build valid SDK `FunctionTool`s and that JSON
schemas avoid constructs the SDK rejects (e.g. arrays must declare `items`).

If the tool should require human confirmation, set `needs_approval` on the `FunctionToolSpec` in the
blueprint (not in the catalog) — it pauses the run and emits `approval.requested`.

---

## 3. Add a database table / column

Read [the cutover section](architecture.md#schema-evolution-cutover-not-migration) first — **there
is no Alembic.**

### New table

1. Define the model in `backend/<feature>/models.py` subclassing `Base` from
   `backend.persistence.database`. Use `JSONText` for JSON columns.
2. If the feature has no `models.py` yet, **add the import to `_register_models()` in
   `backend/persistence/database.py`** — otherwise `create_all()` never sees it and the table is
   silently missing.
3. New tables are created automatically on next startup. No generation bump needed.

### New column on an existing table

`create_all()` does not alter existing tables. Options, in order of preference:

1. **Development only** — delete `local_data/metadata.sqlite3` and restart. Fine for runtime tables.
2. **Ship it** — add the column to `_RUNTIME_TABLES` handling and bump `SCHEMA_GENERATION` in
   `backend/persistence/database.py`. This backs up and rebuilds runtime tables. Only acceptable if
   the data is regenerable (runs, events, sessions).
3. **Preserve data** — follow the `artifacts` / `document_chunks` pattern in
   `_cut_over_schema()` + `_restore_preserved_runtime_dependents()`: rename the old table aside,
   let `create_all()` rebuild, then `INSERT OR IGNORE ... SELECT` the columns forward.

Add coverage in `backend/tests/test_persistence_cutover.py`.

---

## 4. Add a frontend page

**Files:** `src/features/<name>/api.ts`, `<Name>Page.tsx`, `<name>.css`, plus `src/app/App.tsx` and
`src/app/AppShell.tsx`.

1. **`src/features/myfeature/api.ts`** — types come from the generated schema, never hand-written:

   ```ts
   import { json, request } from '../../api/client';
   import type { components } from '../../api/schema.generated';

   export type MyItem = components['schemas']['MyItemResponse'];

   export const myfeatureApi = {
     list: () => request<MyItem[]>('/myfeature'),
     get: (id: string) => request<MyItem>(`/myfeature/${encodeURIComponent(id)}`),
     create: (payload: { name: string }) => request<MyItem>('/myfeature', json('POST', payload)),
     remove: (id: string) => request<void>(`/myfeature/${encodeURIComponent(id)}`, { method: 'DELETE' }),
   };
   ```

2. **`src/features/myfeature/MyfeaturePage.tsx`** — **default export** (App.tsx lazy-imports it).
   Read URL params from `useLocation()`; build UI from `shared/components/Ui.tsx`:

   ```tsx
   import { useEffect, useState } from 'react';
   import { useLocation } from '../../app/router';
   import { EmptyState, ErrorNotice, Loading, PageHeader, Panel } from '../../shared/components/Ui';
   import { myfeatureApi, type MyItem } from './api';
   import './myfeature.css';

   export default function MyfeaturePage() {
     const { pathname } = useLocation();
     const id = pathname.split('/').filter(Boolean)[1];
     const [items, setItems] = useState<MyItem[]>([]);
     const [error, setError] = useState<unknown>(null);
     const [loading, setLoading] = useState(true);

     useEffect(() => {
       myfeatureApi.list().then(setItems).catch(setError).finally(() => setLoading(false));
     }, []);

     if (loading) return <Loading label="Loading…" />;
     return (
       <div className="page">
         <PageHeader eyebrow="Research" title="My feature" description="…" />
         {error ? <ErrorNotice error={error} /> : null}
         {items.length ? <Panel>{/* … */}</Panel> : <EmptyState title="Nothing yet" description="…" />}
       </div>
     );
   }
   ```

3. **`src/features/myfeature/myfeature.css`** — plain classes over design tokens from
   `shared/styles/tokens.css` (`var(--space-4)`, `var(--radius-md)`, `var(--bg-soft)`, `var(--text)`).
   Never hard-code colours; that breaks the 12 themes.

4. **Register the route** in `src/app/App.tsx` — add the lazy import and one branch:

   ```tsx
   const MyfeaturePage = lazy(() => import('../features/myfeature/MyfeaturePage'));
   // …
   else if (pathname.startsWith('/myfeature')) page = <MyfeaturePage />;
   ```

   Order matters: it is an if/else chain and `/` (chat) is the default.

5. **Add nav** in `src/app/AppShell.tsx` — append to the appropriate group in `NAV_GROUPS`
   (`{ to, label, icon, tone }`). Use an existing icon name from `shared/components/Icons.tsx` or add
   one there.

6. **Test** — `src/features/myfeature/myfeature.test.ts(x)` with Vitest (`describe`/`it`/`expect`,
   jsdom environment). Prefer testing pure logic modules over components.

7. **Verify:** `cd frontend && npm test && npm run build`.

---

## 5. Add a run event type

Events are a two-sided contract. Missing either side means the UI silently ignores the event.

1. **Backend** — emit it: `await context.emit("myfeature.thing.updated", payload)` from a tool
   handler, or via the run's event sink from `runs/service.py` / `runtime/hooks.py`. Payload must be
   JSON-serializable (`runtime/serialization.py::to_jsonable` helps with SDK objects).
2. **Frontend** — add the exact string to `RUN_EVENT_TYPES` in `frontend/src/api/events.ts`.
   `EventSource` only receives named events it has a listener for.
3. **Consume it** — handle it in `chatStream.ts` (live state) and/or `chatTimeline.ts`
   (`buildTurnTimeline`), and render in `TurnTimeline.tsx` / `RunInsightsPanel.tsx`.
4. **Test** — `backend/tests/test_run_events.py` and
   `frontend/src/features/chat/chatTimeline.test.ts`.

Use `emit_transient` for high-frequency events that should not be persisted or replayed.

---

## 6. Add a configuration setting

1. Add the field to `Settings` in `backend/core/config.py` with a default and, where sensible,
   `Field(ge=..., le=...)` bounds. Derived paths belong in the `derive_paths` validator; new
   directories belong in `ensure_directories()`.
2. It is immediately settable as `SCHOLARWEAVE_<UPPER_FIELD_NAME>` in the environment or `.env`.
3. **To make it runtime-editable** (Settings UI + `PUT /api/settings`), add it to the schemas and
   logic in `backend/core/settings_service.py` and `backend/core/schemas`-equivalent models used by
   `core/router.py`, then regenerate the API contract and surface it in
   `frontend/src/features/providers/SettingsSections.tsx`.
4. Document it in the configuration table in `README.md` (mark ✅ if runtime-editable).
5. If you rename or retire a setting, add it to `_RENAMED_SETTING_KEYS` or `_OBSOLETE_SETTING_KEYS`
   in `backend/persistence/database.py` so existing databases are cleaned up.

---

## 7. Add a whole new backend feature package

Mirror the existing layout — `backend/workspace/` is the smallest complete example to copy.

```
backend/myfeature/
├── __init__.py      # re-export the service class
├── models.py        # SQLAlchemy tables  (register in persistence/database.py::_register_models)
├── schemas.py       # Pydantic DTOs
├── repository.py    # sessions + queries
├── service.py       # business logic
└── router.py        # APIRouter(prefix="/myfeature", tags=["myfeature"])
```

Then:

1. Construct the repository and service in `create_services()` in `backend/bootstrap.py`, and add
   the service as a field on `ApplicationServices`. If it holds HTTP clients or background tasks,
   close it in `ApplicationServices.close()`.
2. Import and include the router in the tuple in `backend/app.py`.
3. Add the models import to `_register_models()` in `backend/persistence/database.py`.
4. If agents should reach it, inject the service into `ApplicationToolRuntime` in `bootstrap.py` and
   add tools per [recipe 2](#2-add-a-new-agent-callable-tool).
5. `cd frontend && npm run generate:api`.
6. Add `backend/tests/test_myfeature.py`.

---

## 8. Write a test without calling a real model

Two fixtures carry the whole suite.

**`test_settings`** (`backend/tests/conftest.py`) — isolated `data_dir`, `workspace_dir`, and
`frontend_dist_dir` per test under `backend/tests/_runtime/<test_name>/`, cleaned up afterwards.
Use it for anything touching the DB or the filesystem:

```python
def test_workspace_write_is_indexed(test_settings):
    services = create_services(test_settings)
    document = services.workspace.write_file("notes/demo/note.md", "# Title", tags=["demo"])
    assert document.path == "notes/demo/note.md"
```

**`stub_provider`** (`backend/tests/stub_provider.py`) — a real, deterministic OpenAI-compatible
server on a free localhost port, implementing `POST /v1/chat/completions` (with SSE streaming),
`GET /v1/models`, and `POST /v1/embeddings`. **Prefer this over mocking the SDK**; it exercises the
real client, tool-call parsing, and streaming paths.

Point a provider profile at `stub_provider.base_url`, script the replies, and assert on what the
stub received:

```python
def test_agent_calls_search(test_settings, stub_provider):
    stub_provider.tool_plans = [("find research", "web.search", {"query": "quantum", "limit": 5})]
    stub_provider.reply = "Done."
    # …create a provider profile with kind="openai_compatible", base_url=stub_provider.base_url,
    #   run the agent, then:
    assert stub_provider.requests            # inspect prompts and offered tools
```

Follow the closest existing test for the exact wiring: `test_sdk_run_service.py` (runs),
`test_sdk_tools.py` (tools), `test_sdk_api.py` (HTTP), `test_extended_work.py` (multi-agent).

For external HTTP (DuckDuckGo, arXiv, Wikipedia), `monkeypatch` the method on
`ResearchSearchService` — see `test_research_search.py`.

Async tests use `@pytest.mark.anyio`.

---

## 9. Change an agent's prompt or default behaviour

- **Global preamble for every agent** — `backend/agents/instructions.py`:
  `GLOBAL_AGENT_INSTRUCTIONS`, and `current_system_information()` (injects current time in
  `Settings.user_timezone` plus `Settings.user_profile`). Every compiled agent already gets these,
  so do not repeat them in individual prompts.
- **A specific packaged agent** — the blueprint is assembled in that experience's service:
  `autonomous/service.py` (main chat + extended work), `builder/service.py` (agent builder),
  `direct_agents/service.py` (fixed research agents). Shared starting points live in
  `backend/agents/templates.py`.
- **Run limits** — `RunSettingsSpec` in the blueprint (`max_turns`, `max_tool_concurrency`,
  `tracing_enabled`); `ModelSettingsSpec` for `temperature`, `tool_choice`, `parallel_tool_calls`,
  `reasoning`.
- **Structured output** — set `OutputSpec` / `JsonSchemaOutput` on the `AgentSpec`;
  `with_json_schema_output_instructions()` appends the schema guidance automatically.

After changing prompts, run `backend/tests/test_sdk_compiler.py` and the affected experience's test
(`test_extended_work.py`, `test_builder_todos.py`, `test_direct_agents.py`).

---

## 10. Debug a failing agent run

1. **The event log is the source of truth.** Fetch persisted events for the run:

   ```bash
   curl -s "http://127.0.0.1:8000/api/runs/<run_id>" | python3 -m json.tool
   curl -sN "http://127.0.0.1:8000/api/runs/<run_id>/events?after=0" | head -50
   ```

   Look for `tool.failed`, `guardrail.tripwire`, `run.failed` (carries `error`), and
   `context.compacted` / `context.summary_failed`.

2. **Model traffic** — `local_data/llm_calls.jsonl` records every request with credentials redacted.
   Check that the expected tools were offered and what the model actually returned.

3. **Truncated tool output** — a `tool.result_truncated` event means the model saw a bounded version
   (`tool_result_max_tokens`, default 3000). The full payload is an artifact; the agent can read it
   with `read_tool_result`.

4. **Provider problems** — `POST /api/providers/{id}/verify` checks reachability and tool-calling
   support. `GET /api/health` reports SDK version, frontend availability, and OCR availability.

5. **Reproduce without the model** — port the scenario into a test using `stub_provider`
   ([recipe 8](#8-write-a-test-without-calling-a-real-model)) so the failure becomes deterministic.
