# Research tracks

A research track is a persistent local notebook for one research problem. It outlives individual
chat runs. Its research content is ordinary UTF-8 Markdown, stored in:

```text
workspace\
  projects\
    tracks\
      <title--track-id>\
        problem.md
        literature.md
        experiments.md
        results.md
        exploration\
          search-leads.md
          open-questions.md
          hypotheses.md
```

The database stores track identity and paper/conversation associations, not a competing copy of
the document bodies or experiment statuses. Existing papers remain in the library; a paper can
be linked to several tracks without being copied or physically moved.

## Four sections

**Problem** explains what is being solved, why it matters, limitations of current approaches,
expected impact, research questions, constraints, exclusions, and measurable success criteria.
Creation supplies a template, not an LLM-generated claim of novelty or impact.

**Literature** contains one comparison table and detailed linked sections for individual papers.
Include paper/link/year, a 3-4 sentence summary, approach and setup, findings, limitations/gaps,
and relevance. Per-paper sections explain the problem, motivation, contributions, methods,
assumptions, evaluation setup, results, and discussion. Cross-paper synthesis identifies supported
connections, disagreements, and experiments motivated by gaps. Cite underlying paper evidence;
label abstract-only coverage and distinguish author-stated limitations from assistant inferences.
Reuse existing summaries and source files before acquiring or generating duplicate artifacts.
Paper evidence notes follow the same four-part outline as reviewed summaries: motivation,
contribution and novelty; method; results (including limitations); and open questions. Missing
information stays explicitly unestablished, rather than being invented to fill the outline.

**Experiments** records external work. Its canonical tracker table uses:

```markdown
| ID | Experiment | Status | Success criterion | Evidence |
| --- | --- | --- | --- | --- |
| EXP-001 | Measure baseline retrieval | Planned | Record quality and latency | Not yet supplied |
| EXP-002 | Evaluate reranking | Suggested | Compare against EXP-001 | Not yet supplied |
```

IDs are stable and unique. Allowed states are Suggested, Planned, Running, Completed, Blocked,
and Cancelled. Add detailed experiment sections documenting hypotheses, rationale, procedures,
data and metrics, baselines/ablations/controls, dependencies, external code or run references,
and honest outcomes. Assistant proposals remain Suggested until accepted; running and completed
states require reported progress. Keep unsuccessful and cancelled experiments with their reasons.
Use Markdown links such as `[EXP-001](experiments.md#exp-001)` in results.

**Results** keeps supplied observations/measurements and provenance, comparison to baselines,
limitations/uncertainty, assistant interpretation, and open questions distinct. Link every
attributable result to its experiment ID. Unmatched or ambiguous results stay visible with
warnings; no measurements are invented and no outside code is executed by ScholarWeave.

## Exploration

Every track has an `exploration\` folder. Existing tracks receive it at startup without changing
their four documents. It begins empty: the researcher and assistant create as many topic-specific
Markdown notes as needed, rather than filling a mandatory template with invented content.

Use the track's **Exploration** file list to create/open notes, then use the same editor, revision
checks, history, discussion, and single-file import/export controls as for the main documents.
Filenames use up to 80 lowercase letters/digits, hyphens and underscores (for example,
`retrieval-hypotheses.md`). In the terminal, **Exploration** opens the file picker and creation action.

Record questions, tentative hypotheses, search strategies, leads, alternative approaches, rejected
ideas, negative evidence and next steps. Distinguish observations from speculation and cite sources.
Review existing exploration before substantial track research; promote supported conclusions into
the relevant main section without discarding exploratory context. Discussion alone does not save
files. Requested LLM writes use `write_research_track` with `section: "exploration/search-leads"`.
Creating a new file requires `expected_sha256: null`; updating one requires its fresh read hash.
Generic workspace mutations cannot bypass these checks. Files use the existing workspace search index.

Whole-track ZIPs include exploration files and their manifest inventory. Older four-section ZIPs
remain importable and do not delete exploration. Import replaces only files shown in its preview,
leaving other notes untouched. Archives support up to 100 exploration files within the existing
upload and per-file size limits; larger tracks can export notes individually.

## Chats and research skills

Attach a chat to a track, or use a section's discussion action. A discussion action prepares an
unsent message; it does not trigger inference or saving. The existing model, web toggle, and
per-message effort settings still apply. Track metadata identifies relevant files; the model
reads their contents on demand with `read_research_track`. Content imported from files is data,
not a source of new system instructions.

The four bundled recipes are `problem-framing`, `literature-review`, `experiment-planning`,
and `results-interpretation`. They describe evidence requirements and document structure, not
new capabilities or permission to save. Return findings in chat unless saving was requested.
Requested changes use `write_research_track` with the revision from a fresh section read.
Paper membership uses `organize_research_track`; it does not change the paper's own files.

An agent work plan describes tasks ScholarWeave is executing in its current run. A track's
experiment table describes the researcher's external work over time. Planned or Running
experiments must not cause an agent run to continue indefinitely or poll for outside progress.

## Staged literature reviews

Use **Deep Work > Literature reviews** when you explicitly want a saved review document,
rather than an ordinary answer or a canonical single-paper summary. Track pages link to this
dedicated workspace with the track selected. It shows stage progress, usage, source coverage,
pending decisions, and pause/stop/resume controls without occupying main chat. The Deep Work
coordinator can propose a review through `literature_review`; ordinary chat cannot start one.
Scope, constraints and paper/comparison limits require your initial approval. Highly relevant and
relevant papers are then collected and read automatically. Research tasks in the same
workspace expose open-ended coordinator work, steering, stop controls, findings and run history.

An attached track is working context, not just the final save destination. A review started from
a track-linked chat inherits that track when no other destination is selected. Before proposing
scope, it snapshots bounded excerpts of the saved problem, literature, experiments, results,
exploration, and linked papers' canonical summaries/notes. Workspace BM25 helps prioritize relevant
material; long excerpts and omitted files are labelled, not treated as complete coverage.
These are prior-work leads, not verified current PDF evidence. Nothing is saved to the notebook
at this step, and unsaved editor drafts are not included.

The exact excerpts and file hashes are retained in the review's existing job records and evidence
JSON export. Resume uses that original context, so later notebook edits cannot silently change an
approved review. Start a new review when the question or prior-work context has changed.

| Stage | Work and approval boundary |
| --- | --- |
| Scope | One isolated main-model job proposes questions, criteria, searches, and three work limits: papers screened, papers collected/read (including references), and cross-paper comparison passes. Approve these before discovery proceeds. |
| Discovery | Seed the shortlist with linked papers and run approved local/external searches. Track membership alone does not establish relevance. No recursive metadata-reference frontier. |
| Screening | Classify and rank every admitted paper. HIGHLY_RELEVANT and RELEVANT papers are automatically selected; SOMEWHAT_RELEVANT and IRRELEVANT papers are not automatically acquired or read. |
| Reading plan | Automatically acquire selected PDFs within the read limit, reuse extraction, and assign every page a reasoned read/defer decision. The configured decision role owns page choices. |
| Reading | Fresh no-tool workers read bounded source spans, retain verbatim quotations and source versions, and request real follow-up pages within approved papers. |
| References | Extract citation-supported bibliographies only from original papers; count distinct original citing papers. Shared references (more than one original) are automatically looked up on arXiv and screened. |
| Reference collection | Verified highly relevant and relevant references are collected, page-planned and actually read within the same limits. Ambiguous/unresolved identities are skipped with visible notices, not guessed. Supplemental evidence contributes to synthesis, never to recursive reference discovery or original citing counts. |
| Synthesis | Bounded comparisons and reduction produce themes and gap hypotheses, followed by audits against the original quoted evidence. |
| Publication | Save Markdown led by findings and next checks, followed by comparison, four-part paper evidence notes, coverage limits, and source footnotes. Link the local papers to the track; export operational/evidence JSON separately. |

Relevance labels are metadata-based leads, not calibrated probabilities or paper-quality ratings.
Page triage does not categorically skip appendices, ablations, negative results or supplementary
methods. Such pages can contain essential qualifiers and can be requested later. The coverage
viewer distinguishes fully read, partially read and deferred pages. Targeted review evidence never
marks a canonical full-paper summary as complete.
The review does not generate duplicate canonical summaries or overwrite paper notes. Its selective
evidence notes link the existing summary, notes and PDF. Full-paper summary generation remains the
separate, explicitly requested reviewed-summary workflow.

### Models and work limits

Choose the main research model independently from the optional decision role. Supported generative
provider profiles can be mixed: local/local, hosted/local, or local/hosted. **Structured generation**
uses an ordinary generative model through its explicitly selected Chat Completions or Responses API.
Set **Settings > Default models > System One decisions** to have new reviews inherit a native
decision model. An explicit role in review setup overrides that default; existing reviews keep
their saved roles. Without either, the main model screens in batches, not one generation per paper.

For **Native llama.cpp System One**, configure an OpenAI-compatible profile with the server root
or a base URL ending in `/v1`, enable the model's **decision** capability in Settings, declare its actual supported context,
and use Verify. Native calls target `/v1/systemone` on llama.cpp (adding `/v1` for a server-root
base URL), not a chat-completion prompt disguised as a classifier.
Decision-only models can declare contexts below 4096, but are not chat models. Set the review's
**Verified decision input allowance** conservatively; it cannot enlarge the model's declared context.

At a pending approval, **Resume from** can return to an earlier stage without discarding the
review. Returning to discovery or screening rates unfinished candidates and automatically updates
the selected set. The shortlist is read-only: rows expose metadata and abstracts, not approval
controls. Legacy pending paper/reference gates continue automatically on startup or resume.

Small contexts matter: the English Laya model card advertises 512 tokens; model variants differ.
Julia's advertised runtime context is larger, but a long context is not evidence of equally reliable
long-context decisions. ScholarWeave admits complete metadata plus rubric and framing using a
conservative UTF-8-byte upper bound for unknown decision tokenizers. A small model may therefore
fall back frequently. Oversized, uncertain, unavailable, or invalid native decisions go to grouped
main-model work with notices; they do not silently discard candidate papers. Decision models do
not determine scientific truth, research novelty, or the final gap analysis.

The shared inference scheduler defaults to eight same-model native decisions in parallel.
Change **Parallel decision calls** in Settings > Default models from 1 to 32; no restart is needed.
The progress bar receives a live update after each completed screening decision. If the event stream
disconnects, the page reconnects and continues polling persisted state every three seconds.
Generation and model switches remain exclusive. Screening and page triage are grouped by model
role, with interactive work allowed between jobs. Request scheduling
does not manage your GPU residency or guarantee server-side cancellation: when using llama.cpp's
model router, configure `--models-max 1` yourself if only one model may remain loaded.

**Clear review** asks for confirmation, stops its active requests, removes review history and
verified task-owned downloads, and opens fresh setup with the objective retained. Pre-existing,
shared, annotated, and unverified-ownership papers are kept and counted in the result. Exact
published review blocks and unchanged output notes are removed; edited outputs are preserved.
Automatically added track membership is removed only when no remaining literature text or other
review requires it. Older downloads without ownership metadata are not deleted automatically.

The three used/max counters measure distinct admitted screening papers, distinct papers acquired/read
(originals and supplemental references together), and comparison passes. Retries, cached receipts,
page chunks and resumed work do not double-charge a paper. One comparison pass is the genuine
synthesis/reduction/audit workflow comparing the current read set against the research questions;
it is not an N-squared pair count or a charge per model chunk. Changed evidence requiring a new
comparison consumes a new pass. Limits are checked before acquisition/model work, and exhaustion
offers an explicit work-limit extension. Pause/stop remain available. Tokens and context admission
are tracked internally with provenance and conservative interrupted-usage accounting, not exposed
as the user's budget. Existing token-budget reviews migrate without stranding old approvals.
External source pricing/credits remain subject to their providers. Actual speed and quality depend
on your models, hardware, switching cost, and corpus; native decision support has no promised
end-to-end speedup.

Primary references: [llama.cpp decision models](https://huggingface.co/blog/ggml-org/decision-models-in-llamacpp),
[server protocol](https://github.com/ggml-org/llama.cpp/tree/master/tools/server),
[Laya](https://huggingface.co/convaiinnovations/laya), and
[Julia](https://huggingface.co/SupersonicLabs/Julia-1).

### Discovery, approval, and recovery

External discovery can be disabled independently. OpenAlex is optional and can use
`SCHOLARWEAVE_OPENALEX_API_KEY` from your process environment or uncommitted `.env`; restart the
backend after changing it. A compatible API base can be set with `SCHOLARWEAVE_OPENALEX_API_URL`.
This is a source-provider key, not a ScholarWeave account.

arXiv search and downloads share one active request and at least three seconds between starts.
OpenAlex uses conservative 30/minute pacing, cursor pages of at most 100 works, and reference-ID
batches of at most 100. Metadata queries and works are cached; overlapping bibliographies reuse
the same work records. Reference discovery uses original PDF citations only, never recursive expansion.
Rate-limit responses stop that source and retain a cooldown rather than immediately retrying it.
Completed search receipts are reused on resume; resume is continuation, not a fresh literature update.
OpenAlex reference coverage can be incomplete, and this workflow does not claim to parse every
PDF bibliography or exhaust a field.

Shortlist sorting applies across all pages. Newly discovered papers are screened and automatically
selected only at high/relevant ratings within the work limits. Similar titles alone never establish
paper identity; ambiguous arXiv matches are skipped visibly. If a PDF is
unavailable, upload it through the existing library, pause the review, expand that candidate's
details, **Find local copy**, explicitly **Link local paper**, then resume. Linking a source does
not approve an unselected paper.

**Pause review** preserves the current stage. **Stop stage and use valid partial results** advances
only if that stage has sufficient valid output; otherwise it reports that the researcher stopped it
and remains resumable. Neither control bypasses a required approval. Resume continues unfinished
jobs or lets you revisit an earlier stage after a stop/completion. Changed source versions invalidate
old evidence for new synthesis/publication. Work that was running at shutdown recovers when the
single-worker backend restarts; intentionally paused reviews stay paused.

Track publication saves the literature text, paper membership and workspace index together under
the existing revision guard and recovery journal. A failed save rolls them back together. No new
database tables or competing artifact catalog are needed. Publication preserves existing literature
text and annotations; only a completely untouched literature template is replaced with the first
review instead of leaving an empty comparison table above it. Standalone
publication creates a tagged workspace note. Stable publication identities recover a write interrupted
before its checkpoint/index update without creating a second copy or overwriting your edits. Revisited
reviews can append a new edition; footnote IDs are namespaced so earlier editions retain their citations.
Gap hypotheses distinguish author-stated limitations, inferences and coverage gaps, and include
caveats, counter-evidence when present, and concrete next checks. This is a selective review requiring
researcher judgment, not a guarantee of novelty or an exhaustive systematic review.

**Open saved review** opens the literature document, not the track's problem tab. Clean content
reloads when opening a new publication; unsaved annotations are retained for reconciliation.
Relative links to canonical summaries, notes and PDFs open the existing local readers. The
shortlist also links directly to each available local paper.

## Import, export, and recovery

Export a single section as Markdown or a whole notebook as a ZIP. The whole-track package
includes round-trip metadata and preserves section names, IDs, and relative links. Linked
papers are references, not duplicate PDF payloads.

For an external `results.md` or other section file, choose its destination section explicitly.
Preview current and incoming content and review warnings before applying. Merge preserves
existing findings; replacement requires explicit confirmation. Review or edit the proposed
merged document when duplicate experiment IDs or conflicting observations need reconciliation.
Malformed trackers must be corrected before they can be saved.

ZIP import validates member names, size limits, encoding, metadata, and section contents before
writing. It never extracts arbitrary file paths or executes imported content. Applying a preview
checks the current revisions, so a newer application or external edit rejects the stale import
rather than being overwritten. Do not treat an HTTP failure as a successful import.

Every changed section retains its earlier content in recoverable history. Loading an older
version into an editor creates a draft: review it, then save against the current document revision.
History is not indexed as another copy of current research evidence.

Direct external edits are supported, but external editors do not participate in the backend's
mutation lock. Reload track contents and explicitly refresh the existing workspace index after
external changes. Application mutations update the existing SQLite FTS5/BM25 index automatically.

## Terminal interface

`Ctrl+5` or `/tracks` opens the notebook catalog. `Ctrl+N` creates a track; the section buttons
select its four documents, and **Exploration** selects or creates an exploratory note. `Ctrl+E` toggles preview/edit and `Ctrl+S` saves. Unsaved drafts survive
view changes; selecting another section or track requires discard confirmation. Save conflicts
retain the draft. History loads previous content without silently replacing the current file.

Discuss prepares a track-linked chat draft. `/track` attaches or detaches the current chat;
association changes are disabled while its run is active. Import/export opens the local web
editor, which handles file selection, preview/confirmation, downloads, and paper membership.
The terminal accesses all research state through the typed HTTP API.

`/review` lists the current chat/track's staged reviews and offers pause, stop-stage and resume.
It opens the web review panel for model setup, scope/work-limit approval, live per-paper activity
and source-backed findings. Reading/evidence extraction icons name actual work; canonical summary
generation is never implied by selective extraction.
