# Recipes

Read [AGENTS.md](../AGENTS.md) first.

## Paper library UI

Shared UI lives in `frontend/src/shared/styles/`: keep page titles, spacing,
surfaces and controls on the shared tokens instead of introducing a separate
visual system per feature. Preserve the light, sepia and dark palettes and their
theme-matched navigation. Use compact gutters and content-driven card heights rather
than adding empty space to imply hierarchy. Desktop navigation uses full labels; compact
header controls below 1200px preserve room for primary destinations. Below 761px the
primary navigation gets its own full-width row. Sticky readers and assistant panels must
respect `--topbar-height`. Use `PageHeader` for standard page headings and reserve
boxed surfaces for reading/editing, not every navigation group. Model settings
use compact cards without stretching them to match the tallest card. Check populated pages in
light and dark themes at desktop, tablet and mobile widths when changing this layer.

Settings triggers keep a visible label and a 20px gear using the shared
`settings-trigger` style, including the global navigation link. Match the standard
button height of 2.25rem rather than making Settings larger than adjacent actions.
Page settings use the shared `SettingsDrawer`: paper-reader options, secondary Notes filters and maintenance,
Track import/export and assistance options, Brief interests/model/temporary-data controls,
and Research model choices stay out of the primary work surface. Keep the Notes workspace-view
picker visible above search in the Artifact browser. Keep search, creation,
reading, saving and explicit research/generation actions visible. Closed drawers retain
their drafts; Escape, the close button and the backdrop dismiss them without performing
an action and restore focus. Keep actionable errors inside the drawer when it owns the action.
Retain an indication of active filters outside a closed drawer.

Route changes use React transitions to retain the prior page while a lazy module loads.
The shell animates pathname changes without keying/remounting the page or restarting motion
on query-only navigation and polling. Shared drawers transition both the panel and backdrop;
respect reduced-motion preferences and keep the navigation bar stable while content changes.

The library separates folder navigation, filtered paper browsing, and the reader.
The Folder column is hidden by default; the library toolbar can show it and remember
that choice locally without changing folder navigation or saved memberships.
The Papers library is centered at the shared content maximum width, without a large page heading.
Folder navigation comes first, followed by search and compact status/usefulness/sort selectors.
The count, folder-column visibility, List/Topics and Summarize controls share a quieter view row.
These controls stay visible and wrap on small screens. Add paper and Batch OCR sit
in a sticky right-hand action column that wraps above the browser on narrower screens.
Continue-reading cards and their page-progress bars sit below the actions in the same
column. Paper rows and continue-reading cards share a filled, accent-blue reading-progress
bar and percentage based only on the saved page position. Unknown page totals show the
saved page without inventing a percentage. Reading bars use feature-specific classes,
not the shared monitoring `.meter` style. Topic browsing replaces the flat table entirely,
including its column headings and select-all control.
Paper rows show a 280-character
abstract preview below the title, with inline expansion beside file metadata, including search
and topic views. Abstracts reuse stored metadata or an Abstract section in the opening
indexed chunks, ending at the next heading or keywords (or the available text's end).
Without an Abstract label, a substantial opening prose paragraph is used as a heuristic
fallback, skipping short title/author blocks. Extracted previews are bounded to 4,000
characters; missing abstracts are explicit. Listing never downloads or generates a summary.
`frontend/src/features/documents/libraryModel.ts` derives ID and folder indexes from
canonical document records; folder membership does not duplicate or move PDF,
notes, or summary files. Keep search filters aligned with `/documents/search`.
Reader focus hides navigation without resetting filters, and previous/next paper
controls follow the visible page. Summary tools collapse when a PDF is available;
extraction controls stay expanded for papers that need preparation.

Downloading a paper shows an indeterminate progress bar and elapsed time through
download and preparation. Keep its state outside the page instance so closing the form,
browsing another paper or leaving and returning to Papers retains the original elapsed
time without starting another request. Clear progress on success or failure; retain
results and errors until Papers can display them, without redirecting another page
when a download finishes. The download endpoint does not report byte progress or an ETA;
do not invent a percentage or time remaining.

Usefulness is a manual paper label: Unrated, Useful, Maybe useful, or Not useful.
Change it from a paper row or the reader; filter it in the library toolbar. The active
filter stays visible beside search and Clear resets it. Labels persist in document
metadata, separately from preparation status and browser reading progress. Unrated
includes older papers without a label. Filtering applies before search pagination
and to topic browsing; it never deletes papers or automatically excludes them from research.

The Notes artifact browser also exposes the existing storage hierarchy: standalone
notes (`knowledge/`), paper files (`library/`), review outputs (`research/`),
projects (`projects/`) and attachments (`inbox/`). Location and filename/paper/folder
filters operate on the workspace file listing, not a second index. Unknown roots
remain visible under All locations. These are navigation groups, not file moves;
canonical paths and saved links remain unchanged. Focus reader hides the browser
without clearing its filters or the editor draft.

Notes settings includes a confirmed cleanup for standalone and canonical paper notes strictly
smaller than 1 KB (1024 raw bytes). It scans all notes, not only the current filter/page,
previews candidates and rechecks their hashes and byte sizes before deletion. Keep changed,
protected and unreadable files; report skipped/failed files and remove deleted entries from
the existing workspace index. Summaries, generated artifacts and managed track files are
excluded. The currently open note is also excluded, even if saved; close it before cleanup
to include it. Unsaved drafts are never cleanup candidates. After cleanup, an explicit
Create notes or Add to notes action recreates only the missing canonical note through
`POST /api/documents/{document_id}/notes`; ordinary reads never recreate files. Existing
notes, their edits and paper summaries are preserved, and subsequent edits retain revision checks.
Paper-note drafts keep their original paper, path and revision. Closing or switching the
reader's side panel retains the draft; leaving the paper requires discard confirmation.
Appending a quotation does not update an open draft's save revision: conflicting saves keep
the draft rather than overwriting the new quotation.

The Notes editor preserves JSON value types and validates JSON before saving. Generated
receipts, immutable paper versions and managed track sections are not generic editable files;
show their read-only state and direct users to the owning workflow. User-facing workspace
writes/deletes enforce these protections on the server as well. Generic folder deletion also
rejects managed research, paper, attachment and metadata folders, including their descendants;
removing a parent must not bypass receipt protection or leave review recovery pointing at missing files.
Promoting a summary requires explicit replacement consent and the current canonical hash.
Preserve different canonical contents as a readable immutable version before replacement.
Summary drawers recover queued/running work from the existing run store; closing a drawer
does not authorize another run. The service rejects duplicate active summaries for a paper.
Reader navigation follows the current search sequence, and late extraction completion must
not replace a different open paper. Keep extracted text accessible without a PDF and on small
screens; zoomed PDFs must retain reachable left and right edges.

Global search reuses document full-text search, workspace search, and existing track,
review and conversation lists. It searches chat titles/latest-message previews, not
entire transcripts. Show source-specific failures and a retry action alongside partial
results; never present failed search as an empty library. Clear superseded query results,
preserve the active result by identity, and keep keyboard focus inside the search dialog.
Closing restores the trigger's focus. Declined navigation keeps the search open.

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

Assistant/chat drafts are retained per browser tab with their conversation, reading context,
ideas and attachment references. Closing Ask hides the surface without discarding its draft;
expanding to full chat carries the same draft key and conversation ID. Hidden assistant state
must not install navigation guards on unrelated pages. Context-only paper questions must not
implicitly join an unrelated latest chat. Active-run steering is text-only, so pending paper
context requires an ordinary contextual message rather than silently omitted context.
Changing the main model explicitly confirms a new thread without its old transcript while
preserving unsent writing. Retry failed startup reads without throwing away successful data,
and fence send/steer completions by conversation/run identity and draft version.

The Chat/Explore ideas selector and idea-editing tools live in the composer's Options
popover, not in a persistent panel above the message box. Selecting a mode, preparing an
idea-action draft or sending a message closes the popover without resetting the mode or
idea drafts. Options can reopen it during the conversation.

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
The page is one Research workspace, not separate review/task destinations or stacked assistant
panels. Keep a single main composer for the starting question or subsequent messages, not a
separate "Continue research" panel. Put the work plan, progress and findings in the same main
flow as paper decisions and conversation answers; ideas, comparison and paper questions all
reuse that composer instead of opening another assistant.
The starting question stays free of model settings and paper-import controls. A gear on the
right opens a dismissible settings drawer for this question's models or replacement models
when resuming saved work. Closing it preserves model choices and unsent text, restores focus,
and never starts or resumes work. Missing models have an explicit action to open that drawer.
arXiv entry belongs to Papers: the library's Add paper flow collects PDFs, while the Research
Papers view adds metadata to a shortlist without authorizing collection or reading. Do not
duplicate either flow below the starting question. **Add PDF link** in the same shortlist view
accepts a direct HTTP(S) PDF URL and optional title through `add_pdf_paper`. Validate the URL,
preserve existing candidate identities/topics, and defer acquisition to reading approval and
the existing safe downloader; adding a link never authorizes collection. Question starters only fill an
editable draft; they never launch work. Preserve the three-stage look (Confirm scope, Choose
papers, Read & compare) and its next-step surface. Saved research is a searchable navigation
list with visible track-filter buttons, never a pair of dropdowns. Filtering only changes that
list, not the open research or its unsent message. New research explicitly starts in the chosen
track. On narrow screens, the history list can be revealed without hiding or replacing the main
composer. Earlier conversation answers collapse without being discarded.
Use plain-language, state-specific next steps (including paused, blocked and budget-limited work);
keep the matching approval or resume controls inside that next-step area instead of repeating
instructions in another card. Do not show an empty paper reader before discovery. Completed research opens Findings when first
loaded, but polling must not switch the user's chosen view. Topic/search/selected-only filters
never change the reading batch. Research and ideas share the same message box.
Keep pause/resume controls above the review content, not buried in the follow-up form.
Place reading focus, selection count and approval together above the shortlist. Topic exploration
and shortlist expansion are secondary disclosures, not part of an oversized floating action bar.
Paper search and selected-only filtering must preserve selections; clearing selections never starts work.
Pause must attempt to stop both active agent and paper work and report partial failures. New paper
research shares its conversation ID with the investigation; follow-ups carry the selected review's
ID, focus, coverage, pending approval and artifact paths. Reuse existing evidence rather than
starting a second pipeline. Legacy `view=tasks` links must still load the same workspace and history.
Agent-created paper reviews use the configured embedding role and targeted reading; missing
embedding configuration is an explicit error, not a silent legacy full-reading fallback.

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

The web experience is now a question-first research workspace, not a stage control panel.
`LiteratureReviews.tsx` owns setup, the searchable shortlist, in-place source brief reader and
Findings; the standalone Ideas tab embeds `IdeasPanel` using normal chat. Defaults resolve from chat
and embedding settings; missing roles are explicit setup errors. The main model also serves
the brief role. Models are secondary controls. A new question never auto-starts from a URL:
`/deep-work?view=reviews&new=1&objective=...` fills an editable draft only.
Existing full-reading reviews remain read-only history; do not delete saved tasks or files.
The obsolete stage selector, manual resume-stage, automatic six-paper selection and destructive
clear UI are removed. Compatibility APIs remain for persisted jobs and terminal clients.

Persist unapproved paper selections and reading focus per review; filter removed candidates
when restoring a draft. Saved-work navigation must preserve its web-access preference.
The bare Reviews route restores the last opened research using a local browser bookmark
of its review/conversation IDs, track and explicit web preference, including after reopening
the browser. Reload stage, approvals, papers and findings from the existing APIs, not from
a cached progress snapshot. Explicit research links, new-question links and track/objective
drafts take precedence; reopening work must never start a new review or approve a stage.
Budget extension shows usage, current limits and proposed total limits before approval; users
can approve a bounded batch rather than repeating one-paper extensions. Paused/blocked reviews
can resume with replacement main/synthesis models through the existing control API, retaining
evidence and approvals instead of creating duplicate research.

The web setup now sends `embedding_model` (explicit model reference, default batch size 16) and a
summary role. New targeted tasks stop the first discovery wave at 24 unique papers; full/legacy
tasks retain the 100-paper cap. Group title/abstract vectors, label topics, and stop at the `topics` gate. Exploration may repeat
for multiple topics. `find_more_topics` separately authorizes up to 100 additional papers using
new main-model search queries, existing source policies, and the same embedding pipeline. Append
new groups without replacing prior topic IDs, explorations or saved choices. It returns to the
topics gate and never authorizes PDF collection. Only `approve_topics` authorizes PDF acquisition.
Keep unselected candidates, explorations and receipts: approval can select a nonempty candidate
subset within the chosen topics. `revise_selection` returns paused/blocked/completed tasks to the
topic gate; resume must preserve that gate. Preserve recovery for historical cleanup checkpoints.
New web requests send `reading_mode: targeted`; absent persisted fields remain `full`. Approval
can change reading depth, including targeted-to-full, without discarding original brief receipts.
Persist approval feedback as `question_focus` and include it in inference/cache provenance.
Targeted mode prepares and briefs one paper before acquiring the next; retrieve at most six 1,800-character
lexically matched spans, preferring paragraph then sentence boundaries, never metadata as verified evidence. Validate verbatim quotes, admitted
pages and section claim indexes. Save fit, approach, pertinent findings, limitations/unknowns and
reading advice with explicit partial-coverage caveats. Publish a deterministic `provisional.md`
after each brief and expose it through `provisional_path`, plus `brief` and `document_id` on the
paper endpoint. Reuse source-version/model/focus-matched receipts on recovery. Test through the
local provider with `test_targeted_reviews.py`, including progressive visibility and full opt-in.
Every readable selected paper's brief/summary must finish before summary-based inference/reduction
and publication. Record individual retrieval/extraction failures in candidate errors and durable
`skipped-sources.json` receipts, then continue without retrying them during reading. Reports disclose
omitted papers as coverage gaps, never summarized evidence; track publication excludes them. Explicitly
resuming collection retries skipped papers, including researcher-linked replacements. With no readable
papers, block honestly. Storage, model, cancellation, budget/pause and unexpected programming errors
must not be treated as unavailable sources.
Use `reviews/topics.py` for this branch and `test_topic_research.py` for local-stub behavior tests.

Keep every review artifact in its `research/<track>/<review>/` workspace folder. Summary text,
intermediate model outputs and receipts must not be copied into SQLite run/job/FTS payloads.
Workspace indexing for `research/` is metadata-only even after refresh or editor saves. Use
SafeStorage, retain provenance/hash checks and cached work, and preserve edited files during cleanup.
PDF copies use `write_workspace_document` and the document upload limit, not the smaller generic
artifact limit; document writes remain atomic. Keep text and receipt size limits unchanged.
The main model's scratch notes and final report cite paper IDs; never describe summary-based
inferences as audited original quotations. The UI leads with the question, explicit paper choice and one next action. Topic exploration asks
the model for reasoned reading recommendations by title; it never preselects a round-robin batch.
Incoming briefs render progressively. Completed batch synthesis is exposed inline by
`GET /api/literature-reviews/{id}/understanding` as a typed snapshot with focus and covered paper IDs.
`refine_understanding` is a revision-guarded control that compares available current-focus briefs,
uses the normal bounded model runtime and comparison allowance, and caches identical inputs.
It runs through the existing task runner, supports pause/retry and budget continuation, and keeps
the previous snapshot visible until replacement. Comparisons spanning multiple context packets
are labelled groups, not falsely presented as a global comparison. New focus/selection is explicit;
follow-up reading reuses the topic gate with a chosen paper subset and question. No per-paper
model synthesis is added. Run `test_targeted_reviews.py`, including the real-provider endpoint test,
and frontend workspace tests when changing this contract.

The following source-audit rules apply to legacy specifications without `embedding_model`.

Pause before changing a review's main model, then send an explicit `main_model` with the existing
revision-guarded `resume` control. Validate all supplied model roles before saving any changes.
Keep collected papers, summaries, source-verified evidence, job provenance and approved work limits;
only unfinished requests use the replacement model. Switching models must not charge another
comparison pass, reset source-page admissions/extensions, or bypass pending approvals. Legacy API
and terminal `/review` clients retain this control; it is not exposed in the new web workspace. Reuse completed jobs only when
their logical task, prompt and input hashes match, and revalidate structured evidence.

For separate-summary-model synthesis, preserve the durable `analysis-plan` job: summary batch
selection, page/reason/status entries and distinct-page admissions survive pause/resume.
Enforce the initial 20-page cap and explicit 20-page extensions on original source reads,
including follow-ups. At the `analysis` gate, **Write report with current evidence** skips
pending reads, not source audits; it requires verified evidence and records incomplete coverage.
The live activity UI pages through saved jobs and loads outputs on demand. Keep its source-page
and summary-batch bars distinct from whole-report completion.
For large summary-selection batches, keep the configured main-model context and normal output
reserve. Unknown compatible model names use model-scoped provider tokenization when available;
do not substitute UTF-8 bytes for actual token counts or infer a tokenizer from a model name.
Keep the packet admission and isolated harness context checks aligned, and test a real stub
request with over 60K input tokens fitting a 128K window. Unsupported tokenization must remain
visible; invalid counts cannot authorize an oversized request.
Tokenize an already serialized harness request directly: wrapping it in another user message
duplicates instructions and inflates escaped page-decision JSON enough to reject admitted batches.

The legacy API **skip_references** control durably disables reference expansion for a review,
including across resume and restart. Before that phase it leaves initial-paper work running;
during reference extraction, screening or collection it cancels and awaits active work before
continuing to synthesis. Completed work is retained, and scope/work-limit approvals for the
remaining initial-paper and synthesis work are not bypassed.

Deep Work progress bars show observed paper throughput (papers/min and papers/s) and
task-local remaining-time estimates after new completions are observed. These are not
model-request RPM/RPS. Measurements start when the page opens and reset on pause/resume
or counter rollback; waiting time is included. Stages without a fixed total cannot
provide a completion estimate.

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
retain durable usage and provenance, and use the shared inference scheduler. Topic-led generation
sends a native JSON Schema to Ollama, while OpenAI-compatible Chat Completions and Responses
retain the named strict schema. Native System One
decisions may run in parallel (the persisted `decision_parallel_requests` limit, default 8, range 1–32,
for the same profile/model). Explicit same-model review-summary groups use their saved
`parallel_requests` limit (default 1, range 1–32); when the summary role names the exact main
provider/model binding, they use the review's saved `main_parallel_requests` limit instead
(default 1, range 1–32). Ordinary main-model stages remain sequential and model switches stay
exclusive. Summary groups finish missing page plans first, cancel/await/account every child,
and keep parallelism out of content-cache keys.
Clear must cancel/await all children before deleting owned artifacts, preserve
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

Reader-created highlights are parsed by `backend/workspace/highlights.py` from canonical
paper notes, including the earlier page-attributed quote format. New PDF captures have stable
highlight markers; explicit connections are stored in the same Markdown file and read in both
directions. `WorkspaceService.reader_notes` projects the existing workspace index, and general
workspace search includes one-hop connected highlights after lexical matches, respecting filters
and pagination. Generated summaries are never reader highlights. Use the revision-guarded
connection endpoint and `ReaderNotesPanel` for the reader and track views; test with
`test_reader_notes.py`, storage/API tests, and the affected reader/track tests.

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
Frontend changes require focused feature and affected-consumer tests, then `npm run build` once
the change is complete; follow the validation scope in `AGENTS.md` for full-suite checks.
Terminal changes need the focused TUI tests.
Saved review links must open the literature document and refresh clean content without replacing
unsaved drafts. Render relative workspace links with their source file path so canonical summaries,
notes and PDFs open through the existing local readers.

Track **Add existing** reuses paper membership and the review service's revision-guarded
`PUT /literature-reviews/{id}/track` operation. Moving a review requires it to be idle and preserves
its artifact paths and original publication receipts; later publication retains separate receipts
for each destination. Reader highlights derive from linked paper membership, never copied files.

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

`default_model_references.small_tasks` is an optional helper paired with the main model.
Use it only for known lightweight operations: library/research topic naming, paper summaries
and briefs, and conversation-context summaries. Never infer task size from user keywords or
model names; chat (including Quick), research planning, comparisons, and final answers retain
their main model. Explicit task selections win. `summary` and `compaction` defaults override
the small-tasks default for their respective jobs; unset helpers retain existing main-model behavior.
Use the same provider profile and exact router model IDs for llama hot-swapping; do not add
load/unload calls or bypass the inference scheduler. Saved runs and review roles keep their
chosen bindings; settings changes affect new jobs. Surface invalid helper selections rather than
silently sending summary/naming work to the larger model. Context maintenance retains its explicit
failure event and main-model fallback. Test defaults, overrides, clearing, and request model IDs.

Settings failures must render a retry action, not remain behind a loading screen. Refreshing
provider metadata must not replace an unsaved settings draft. Discovery is optional: users
can register a known model manually. Verification shows individual capability results and
diagnostic detail rather than treating reachability as proof of tool support.
Archiving warns about affected configuration and can be reversed through the archived-provider
list and restore endpoint without changing the provider ID. Invalid context-window edits
remain explicitly unsaved until corrected or reverted.

Profiles explicitly select `api_mode` in their existing configuration JSON. Keep Chat Completions
as the default for older profiles and compatible servers; never route by model-name keywords.
Ollama does not support Responses. Preserve unset configuration fields when updating a profile.

Request timestamps are projected into the associated user message as
`[Application context: request submitted at <date> <time> <timezone>]`, followed by the original
content. The harness also projects older saved clock entries this way in both protocols, without
rewriting stored user text or moving actual system/developer instructions into user content.
Do not reintroduce mid-conversation system messages for request clocks.

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
The Papers **Batch OCR** action starts a server-owned batch for the current selection/filter.
The **Runs** page shows batch history, aggregate paper/page progress bars, individual paper page counts,
errors and start/stop controls; mounting it must never start OCR.
The active batch lists only currently running papers; aggregate counts and page progress still
include every paper. Keep only the two newest inactive batches in a collapsed history sidebar,
rendering their paper details on expansion. Older batch receipts remain stored.
Surya papers run concurrently up to the configured page-request budget, while the shared page limiter bounds aggregate model
requests across papers. Tesseract papers remain sequential. Recheck eligibility before starting:
complete OCR is reused, incomplete extraction remains retryable, and existing active ingestions
must not be duplicated. Persist successful OCR pages immediately as source-hash-bound page
checkpoints, including layout and figure crops, before reporting page completion. Retrying a batch
or automatic ingestion reuses those pages (and successful pages in older manifests), processing
only missing/failed pages even after cancellation or restart. Embedded-text fallback is not successful
OCR. Explicit **Re-run with OCR** still reprocesses every page. Checkpoints are separate from published
extraction, so incomplete attempts must not replace readable saved content.
`documents/indra.py` provides optional CPU quick extraction and failed-Surya-page recovery.
Do not replace the configured batch engine: reader `mode=quick` selects Indra only for that request,
and persisted `ocr_indra_fallback_enabled` opts into recovery after Surya's existing retries fail.
Load pinned, predownloaded local models lazily; never start a model download during ingestion.
The shared Indra limiter allows one page at a time, with at most four ONNX threads, off the event loop.
Keep CPU engine provenance, quality warnings, recovery reasons, figure crops and page checkpoints.
Do not claim CPU output is verified, math-accurate or equivalent to Surya. Cover both paths in
`test_indra_ocr.py`; tests requiring model weights may skip when those optional local files are absent.
Explicit full OCR can upgrade CPU pages; ordinary retries must continue to reuse them.
A failed OCR page with readable embedded PDF text is saved alongside
successful OCR pages, with its page number and failure recorded in the manifest and document
metadata. Mark that paper and batch `completed_with_errors`, show the affected pages in Runs,
and keep it eligible for retry. If any page has neither OCR nor readable embedded text, preserve
the previous extraction instead of publishing a replacement with missing pages.
Persist job receipts through safe storage so reloads retain progress;
recover interrupted jobs honestly without automatically starting them. Stopping cancels owned
active ingestions, does not start pending ones, and must settle before reporting the job stopped.
Active Runs progress also shows overall page throughput, observed seconds per page, and estimated
time remaining for the batch; individual papers have counts and statuses, not bars or forecasts.
Measurements use only page completions observed in the last two minutes while the page is open
(or the shorter observation time during warm-up), include waiting time, and restart after
navigation/reload. No completions in the window withholds the ETA. Batch estimates use
aggregate page throughput (already accounting for concurrency) and exclude unfinished pages from
failed, skipped or stopped papers. Unknown remaining page totals withhold the batch ETA; stopping
or settled runs show no forecast. These are page-processing estimates, not guarantees for final indexing.
Saved summaries include rendered PDF page images only when the selected summary model explicitly
declares `vision`. Images share the model context budget with extracted text. Saved visual coverage
records the included pages separately from text coverage; incomplete visuals must be disclosed.
Reader questions include a bounded extraction of the current page. Paper page/chunk/search tools
attach the corresponding stored layout, bounding-box coordinate system, reading order and figure
positions through `DocumentService.paper_context`. Declared vision models also receive registered
figure crops; pages without crops use a rendered source PDF page. Text-only models receive no image
payloads, including when replaying history from a vision model. The harness projects tool image
content into multimodal user evidence after all pending tool results, for both Chat Completions
and Responses; never serialize pixels into tool-result text. Saved summaries retain extracted layout
alongside their page images; staged-review source packets use the same evidence builder and reserve
context space before packetization. Missing images and omitted visual/layout coverage remain explicit;
do not re-ingest a paper merely to expose existing extraction.
`documents/surya.py` implements the local Surya 2 OpenAI protocol, structured HTML conversion and
figure cropping. Use the upstream training-time prompts verbatim; a generic Markdown prompt is not
equivalent. Full-page layout/OCR is the primary path, with layout plus cropped-block OCR on malformed,
repetitive or truncated output. The shared page limiter bounds all concurrent paper ingestions;
PDFium rendering remains serialized because its native library is not thread-safe.
Add `test_surya_ocr.py` for changes here, including abstract boundaries, reading order, Markdown,
figure geometry/artifacts, fallback, cancellation and concurrency. Use the local stub provider,
not a mocked model harness. Keep relative figure links portable in `extracted.md`; the paper reader
maps only registered image artifacts to API URLs. Do not delete previous Surya extraction before a
replacement has been produced, and never clean up canonical notes or immutable summaries.
Label legacy model-name settings as restricted fallbacks, not general overrides: enhancement
uses its fallback only without a default vision reference, and the triage fallback is Ollama-only.
Clearing these optional OCR model names deletes their settings rows rather than storing SQL NULL.
Repeated settings saves must work without a restart; apply runtime changes only after commit succeeds.
Normalize malformed UTF-16 surrogate code units at native extraction, before formatting, indexing
or serialization. Restore valid pairs, replace unpaired units with U+FFFD and log the affected page;
keep ordinary Unicode and original PDF bytes unchanged.

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
5. Run the new page's tests and affected navigation/router tests together with
   `npm test -- <feature-filter> <router-test-filter>`, then `npm run build`.
   Follow the validation scope in `AGENTS.md`; changes to shared routing require the full frontend suite.

The app uses a small custom router, plain React state, and CSS design tokens.

## Change Morning Brief behavior

- `backend/brief/catalog.py`: official sources and editable topic phrases.
- `backend/brief/collection.py`: RSS/Atom, stable GitHub releases and lab-index adapters.
- `backend/brief/service.py`: single-flight collection, transient previews, keep/discard/clear.
- `frontend/src/features/brief/`: headline-first page and temporary reader.

Only an explicit generation action starts model-written highlights. First-visit collection,
cached reads and refresh remain metadata-only and must not block navigation. Unsaved interest
edits still need discard protection; saving them must preserve a separately changed Brief model.

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
