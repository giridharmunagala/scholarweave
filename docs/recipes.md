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
4. Classify read/mutation actions in `backend/tools/policy.py`; only safe reads are retried.
5. Bind the catalog ID in the appropriate blueprint in `backend/conversations/turns.py`.
6. Add a focused test in `backend/tests/test_agent_tools.py` and check packaged prompt guidance.

Strict schemas must set `additionalProperties: false` and list every property in `required`.
Represent optional values with a nullable type.

## Change chat effort or research behavior

- Main research prompt: `backend/prompting/defaults/prompts/research.md`
- Turn-based chat boundary: `backend/prompting/defaults/prompts/chat-turn.md`
- Deep Work coordinator: `backend/prompting/defaults/prompts/deep-work-coordinator.md`
- Focused worker: `backend/prompting/defaults/prompts/deep-work-worker.md`
- Blueprint/tool assignment and delegation: `backend/conversations/turns.py`
- Model/tool loop, delegation, and policies: `backend/agents/harness.py`
- Epoch continuation: `backend/runs/service.py`

Use the local stub provider for behavior tests. Script real tool calls through it rather than
mocking the compiler or the harness.

The web UI separates Auto/Quick chat from Thorough tasks on `/deep-work`. Keep review setup,
human decisions and long-running controls on that dedicated page, not in chat or the track editor.
Reuse existing review/task APIs and observability helpers; navigation must not cancel work.
Keep old task/history links accessible. New chat blueprints must not launch staged reviews.

At the API level, Auto, Quick, and Thorough are per-message effort settings; never infer them from persistent
conversation kind. Quick retains local files, attachments, and optional web access. Let the main
model judge intent; do not add a classifier call or keyword routing. Test one-model-call discussion,
switching effort in an old Deep Work chat, and continuation of pending plans at every effort.
New chat titles are derived from the first message without a model/tool call.

`research` is the default response style in both chat and Deep Work. Keep execution capability
separate from per-turn response style: only explicit `review` requires saved paper summaries and
notes. Response styles and the old bounded-web shortcut are legacy API controls, not UI modes.
Summaries requested in chat need not be saved. Test saved-summary discussion, candidate screening, requested writes, supplied citations,
and steering as well as explicit review enforcement. Persisted older run snapshots retain their
original completion policy; prompt changes apply to newly compiled turns.

## Change staged literature reviews

Isolated schema/evidence validation failures get at most two automatic repairs with precise
feedback and separate durable, usage-accounted receipts. Repairs obey the same context,
allowance, inference scheduling, pause and cancellation controls as the original request.
Invalid evidence is never published. Exhausted page grouping alone uses a noticed, recorded
`FALLBACK` to bounded source-order packets retaining every admitted page; it does not synthesize
evidence. Other exhausted invalid outputs block honestly. Explicit resume can start another
bounded attempt cycle. Startup continues only the exact legacy blocked grouping error once,
without changing approved budgets or restarting paused reviews.

- `backend/research/reviews/`: revisioned lifecycle, approvals, cached discovery/references, bounded
  jobs, source evidence, synthesis audits and publication.
- `backend/providers/decisions.py`: native System One admission/protocol/answer validation.
- `backend/research/search.py` and `sources.py`: shared request-lifetime pacing and guarded acquisition.
- `frontend/src/features/reviews/` and terminal `/review`: the same review API and human decisions.

Keep scope and budget approval separate from automatic paper selection. Only HIGHLY_RELEVANT
and RELEVANT originals and confirmed shared references are automatically collected and read.
Replacement sources invalidate receipts; ambiguous identities are skipped with visible notices. Do not expose approval or budget extension through model
tools. Stop-stage must either advance with valid partial output or report an explicit resumable stop.
Do not mark targeted evidence as a canonical full-paper summary.
Page admission for already relevant papers is based on substantive content and section function,
never the review objective. Read abstract/introduction/core methods/main results/discussion/conclusion
and meaningful limitations; generally defer peripheral appendices, extra ablations and trailing
diagram-only pages, but read uncertain pages or the only core evidence. Detect reference lists
independently for the existing one-pass shared-reference flow; reconsider only isolated holes.
Version page-policy caches when these criteria change. The main model groups admitted pages into
bounded source packets, writes source-grounded partial summaries in the shared four-section outline,
then combines and audits a selective per-paper summary. Keep original quotes, source versions and
receipt IDs; invalidate summaries when their receipts change. Show actual partial/combined prose
in live paper views and publication, without touching canonical full-paper summaries.

Every model job must fit its role's declared input/output/context limits, use a fresh transcript,
retain durable usage and provenance, and use the shared inference scheduler. Only native System One
decisions may run in parallel (the persisted `decision_parallel_requests` limit, default 8, range 1–32,
for the same profile/model); generation and model switches
stay exclusive. Clear must cancel/await all children before deleting owned artifacts, preserve
shared/edited/unknown-ownership files, and retain resumable cleanup state on errors. Keep source versions
and verbatim page evidence through reduction/audit. Reuse prior query/job receipts; do not retry
rate-limited sources immediately or replace existing workspace indexing with a review JSON index.
Publication must survive interruption before its state/index checkpoint without duplicate files.
Keep track context in bounded, hash-labelled review receipts and reuse the original snapshot on
resume. Linked papers seed discovery and are screened before automatic reading. Existing summaries
and notes are prior-work leads, never substitutes for current source evidence. Track publication
must journal literature text and paper membership together, reuse canonical source links, and
preserve annotations. Keep selective paper notes on the same four-part outline as reviewed
summaries without claiming full coverage or generating a duplicate canonical summary.

Run `test_literature_reviews.py` with relevant source/provider/tool/run/storage tests and the
architecture guard. Use the actual local stub endpoints, not a mocked harness. Regenerate contracts
for schema changes; run frontend tests/build and focused TUI controls.

Reference workflows must test distinct-original-paper counts across resume, source-version
invalidation, isolated page-gap reconsideration, automatic relevant original/reference reading,
low-relevance exclusion, budget gates and automatic ambiguous-identity skipping.
Never expand references from supplemental collected papers. Inspect captured page reasons,
reference quotes and unresolved matches in Deep Work, not just the running indicator.

## Change workspace discovery or search

- `backend/workspace/layout.py`: canonical paths; never construct paper paths in consumers.
- `backend/workspace/upgrade.py`: explicit offline changes to existing research-file locations.
- `backend/workspace/router.py`: notes, canonical summaries, search and index endpoints.
- `backend/workspace/service.py`: safe files, collection kinds, metadata-preserving refresh.
- `backend/workspace/repository.py`: persistent FTS5 index and BM25 ordering.
- `backend/tools/runtime.py`: `list_workspace`, `search_research_notes`, `workspace_index` handlers.

Reuse `GET /api/documents` for the paper library and `/api/documents/{id}/summaries` for immutable
versions. Do not duplicate them in a second catalog. Workspace text search is separate from cited
PDF chunk retrieval. Normal writes/deletes update FTS transactionally; refresh reconciles external
edits and deletions. Test relevance ordering, pagination, restart persistence, metadata preservation,
and failed-refresh rollback in `test_storage.py`, then API and tool wiring tests.

## Change connected note-taking behavior

Keep canonical paper notes in the existing paper `notes.md`; standalone topic notes live in
`knowledge/`. Do not replace the paper-summary pipeline or create another notes catalog.

Discovery uses the existing workspace FTS5/BM25 index. Have the main agent issue focused queries
for the subject and relevant related concepts, read promising destinations, and distinguish
lexical search results from source evidence. Do not add keyword routing or assume search is
exhaustive.

An explicit note-saving request can add detailed material to a primary note and concise,
source-attributed connections to directly related existing standalone notes. Respect narrower
user scope, avoid duplicate additions, and do not recursively update the library. Discussion
alone does not authorize saving; related-topic discovery does not authorize editing other
papers' canonical notes or summaries.

Prefer additive writes. Narrow corrections must identify exact, unique existing text; do not
regenerate a whole note to add a detail. Preserve old observations and metadata, report partial
cross-note failures explicitly, and keep UI, terminal, and agent write behavior aligned.

Cover preservation of earlier details, related-note additions, single-note scope, discussion
without writes, ambiguous selections, and stale edits. Use the local stub provider for behavior
tests, then run storage/API tests and the architecture guard.

## Change research tracking

- `backend/tracks/`: track identity/associations, Markdown templates and experiment projection,
  revision-guarded section writes, history, import/export and HTTP endpoints.
- `backend/workspace/layout.py` and `service.py`: canonical paths, indexing, and managed-file guards.
- `backend/conversations/turns.py`: attached-track metadata and persisted per-run association.
- `backend/tools/catalog.py`, `runtime.py`, `policy.py`: aligned track read/write/organization tools.
- `backend/prompting/defaults/skills/`: the four section recipes.
- `frontend/src/features/tracks/` and `scholarweave_tui/`: notebook presentation and editing.

Markdown is authoritative; do not add a second experiment-status catalog. Linked papers keep their
canonical files. Distinguish long-lived external experiment states from the current agent work plan.
Each track owns an `exploration/` directory of Markdown notes. Reuse track read/write tools,
revision guards and history with `exploration/<filename>` document keys rather than adding another
tool surface or allowing generic workspace writes. New-file revisions are explicitly null, never
an overwrite exemption. Include exploration inventory in ZIPs, preserve legacy imports, and test
creation rollback/recovery as well as updates.
Test stale saves, externally edited files, revision history, paper reuse, import preview/confirmation,
ZIP validation, round-trip preservation, and track-aware chat using the local stub provider.
Run track and relevant storage/API/agent tests with the architecture guard; regenerate contracts.
Frontend changes require `npm test` and `npm run build`, and terminal changes need the focused TUI tests.
Saved review links must open the literature document and refresh clean content without replacing
unsaved drafts. Render relative workspace links with their source file path so canonical summaries,
notes and PDFs open through the existing local readers.

## Add an instruction-only skill

1. Write one UTF-8 Markdown file in `backend/prompting/defaults/skills/<name>.md` for a bundled
   recipe, or `local_data/skills/<name>.md` for a local recipe. See [skills](skills.md) for the format
   and limits; local files override bundled names.
2. Explain when to apply it and how to use existing tools. For substantial multi-step tasks,
   reference the existing work-plan tools; do not create tools, scripts, or a separate checklist.
3. Preserve source attribution, per-turn capabilities, and explicit save/delete permission.
4. Test snapshots and tool workflows in `backend/tests/test_skills.py` with the local stub provider,
   then run `test_prompting.py`, `test_research_modes.py`, and `test_architecture.py`.

## Add a delegated sub-agent

1. Add the delegate agent to the blueprint's `agents` list.
2. Add an `agent_tools` entry with `owner_agent_id`, `delegate_agent_id`, `tool_name`,
   `tool_description`, and an optional `max_turns` or `serialize_calls`.
3. Keep the chain at most two levels deep; the compiler rejects a third level and any cycle.
4. Remember the sub-agent receives only the `request` string, so its prompt must be self-contained.

## Change a provider generation protocol

Profiles explicitly select `api_mode` in their existing configuration JSON. Keep Chat Completions
as the default for older profiles and compatible servers; never route by model-name keywords.
Ollama does not support Responses. Preserve unset configuration fields when updating a profile.

Wire selection through `providers/schemas.py`, `service.py`, `runtime.py`, and `binding.py` into
the shared harness request/stream adapter. Direct vision/generation helpers must honor it too.
Responses uses `store=false`, full local input, encrypted reasoning replay scoped to the same
binding, and only application function tools. Keep reasoning overrides explicit; do not silently
clear them or fall back to another endpoint on failure.

Use the real local stub's `/v1/responses` endpoint to test reasoning-plus-function round trips,
local history, usage/cache accounting, provider verification, and image/structured generation.
Cover stream errors, cancellation, incomplete-output recovery, workers and context maintenance
through the harness tests. Run provider/harness/context tests and the architecture guard, regenerate
contracts, and check both web and terminal provider selectors.

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

## Change Morning Brief behavior

- `backend/brief/catalog.py`: official sources and editable topic phrases.
- `backend/brief/collection.py`: RSS/Atom, stable GitHub releases and lab-index adapters.
- `backend/brief/service.py`: single-flight collection, transient previews, keep/discard/clear.
- `frontend/src/features/brief/`: headline-first page and temporary reader.

Use the existing public-network/size-validated downloader, never a separate unrestricted HTTP path.
Do not import or extract papers while collecting headlines or downloading previews. Keep alone
persists through `DocumentService` or `WorkspaceService`; clear/discard cannot delete library data.
Changing an arXiv revision invalidates its old temporary preview. When a saved PDF is reused, label
that it may be an earlier revision. Keep explicit errors, unknown dates, revision labels and source
limits visible. Source-marketing filters are editorial policy, not a scientific quality assessment.
Run `test_brief.py`, `test_source_downloads.py` and `test_architecture.py`, regenerate API contracts,
then run frontend tests/build. Exercise headline -> preview -> keep -> clear, plus unsaved clear,
daily expiration, invalid feeds, size limits, failed downloads and source-format changes.

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
