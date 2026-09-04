# Recipes

Read [AGENTS.md](../AGENTS.md) first.

## Add or change an API endpoint

1. Put wire models in the owning feature's `schemas.py`.
2. Put behavior in its service.
3. Put SQL in its repository only.
4. Keep the router thin and resolve the container with `Depends(services)`.
5. Run the closest backend test.
6. From `frontend/`, run:

```powershell
npm run generate:api
npm run check:api
```

Never hand-edit generated contracts.

## Add an agent-callable tool

1. Add one six-field entry to `APPLICATION_TOOLS` in `backend/tools/catalog.py`.
2. Add the named `(arguments, context)` handler to `ApplicationToolRuntime`.
3. Add `backend/prompting/defaults/tools/<catalog-id>.json` with guidance for every parameter.
4. Bind the catalog ID in the appropriate blueprint in `backend/conversations/autonomous.py`.
5. Add a focused test in `backend/tests/test_agent_tools.py`.

Strict schemas must set `additionalProperties: false` and list every property in `required`.
Represent optional values with a nullable type.

## Change chat or Deep Work behavior

- Main research prompt: `backend/prompting/defaults/prompts/research.md`
- Deep Work coordinator: `backend/prompting/defaults/prompts/deep-work-coordinator.md`
- Focused worker: `backend/prompting/defaults/prompts/deep-work-worker.md`
- Blueprint/tool assignment and delegation: `backend/conversations/autonomous.py`
- Model/tool loop, delegation, and policies: `backend/agents/harness.py`
- Epoch continuation: `backend/runs/service.py`

Use the local stub provider for behavior tests. Script real tool calls through it rather than
mocking the compiler or the harness.

## Add a delegated sub-agent

1. Add the delegate agent to the blueprint's `agents` list.
2. Add an `agent_tools` entry with `owner_agent_id`, `delegate_agent_id`, `tool_name`,
   `tool_description`, and an optional `max_turns` or `serialize_calls`.
3. Keep the chain at most two levels deep; the compiler rejects a third level and any cycle.
4. Remember the sub-agent receives only the `request` string, so its prompt must be self-contained.

## Change paper ingestion or OCR

Relevant files:

- `backend/documents/ingestion.py`
- `backend/documents/formatting.py`
- `backend/documents/ocr.py`
- `backend/documents/vision.py`
- `backend/tests/test_documents.py`
- `backend/tests/test_tesseract_ocr.py`

Preserve the source PDF and canonical workspace files. Test native text, selective OCR, forced OCR,
OCR failure with native-text fallback, and image-only failure behavior.

## Add a database table or column

New table:

1. Add the ORM model to the owning package.
2. Ensure `_register_models()` imports that package.
3. Add repository tests.

Existing-table column:

1. Read `backend/persistence/database.py`.
2. Decide how existing local data is preserved.
3. Update the explicit cutover generation and its tests when required.

`create_all()` does not migrate existing tables.

## Add a frontend page

1. Add `frontend/src/features/<feature>/<Page>.tsx`.
2. Add typed API calls beside the feature.
3. Lazy-load and map the path in `frontend/src/app/App.tsx`.
4. Add navigation only when the page is a primary destination.
5. Run `npm test` and `npm run build`.

The app uses a small custom router, plain React state, and CSS design tokens.

## Add a run event

1. Emit it through the run event sink.
2. Add the exact name to `RUN_EVENT_TYPES` in `frontend/src/api/events.ts`.
3. Update timeline/stream projection tests.

Named SSE events not registered by the browser are silently missed.

## Debug a failed run

1. Inspect the run record and persisted events through the API.
2. Inspect `local_data/run_logs/<run-id>.json`.
3. Look for the first tool or epoch failure, not only the terminal message.
4. Reproduce with the smallest test using `backend/tests/stub_provider.py`.
5. Fix the producer, then run the focused test and architecture guard.

Operational snapshots intentionally omit full prompts, user input, model output, and tool payloads.
