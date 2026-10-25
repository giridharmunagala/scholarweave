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

## Add existing research

Open a track and choose **Add existing** to link a library paper or attach an existing saved
review. A review belongs to one track; moving it from another track requires confirmation.
Pause running reviews in Deep Work before moving them. Revision checks reject stale moves.
The **Linked reviews** list opens the saved task and its report. Reassignment preserves the
original files, prior-work snapshots, published notebook entries and existing paper links;
it does not move files or rewrite earlier findings. Link a review's papers separately when
you want their personal notes in the track.

Linking a paper includes its existing personal notes automatically. **Reader highlights**
shows page-attributed passages saved with the PDF reader's **Add to notes** action, not generated
summaries. The same notes remain available in every track linked to that paper. Removing a paper
from a track does not delete its notes or their connections.

In a paper's Notes drawer, open **Highlights & connections**, or use the track's
**Reader highlights** view. Choose **Connect highlight**, search highlights from any paper,
and select **Connect this highlight**. Connections work in both directions and links open the
original paper at the quoted page. They are stored alongside the original highlights in the
canonical `notes.md`; no copied notes or separate notes catalog is created.

Highlight search and the existing workspace Notes search use the SQLite FTS5/BM25 index,
then include directly connected highlights, with connected matches labelled explicitly.
Search follows one connection, not an unbounded chain. Track search stays within its linked
papers; opening a highlight's connections can show notes from other papers. Existing kind/tag
filters and pagination still apply. Old page-attributed reader quotes are recognized without
rewriting the files; new captures have stable identifiers. External file edits require the
existing workspace index refresh, and stale connection writes preserve the file and report a conflict.

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

**Explore ideas** opens lightweight chat from any topic, independently of tracks or
completed reviews. The composer’s intent selector switches between ordinary chat
and conceptual ideation. Start with a question; a paper passage or existing findings
are optional context, not prerequisites for downloading papers or creating full
summaries. Compare 3–5 different directions, then ask to expand, combine, challenge,
simplify, or change direction. Observations, speculation, assumptions and unknowns
stay separate, and novelty remains unverified. Discussion creates no saved artifact.
The reusable Ideas workspace uses the same chat engine, with an editable question and
bounded, readable evidence context. Edit **Selected idea**, then **Develop**, **Challenge**,
**Combine** (with a second direction), or **Simplify** to prepare a draft. Nothing sends
until you press Send. Selection edits are retained locally for that conversation;
sent actions also preserve the selection in ordinary conversation history.
**Check against literature** opens a **new editable review** carrying the selected idea,
question and supplied context as text, not opaque paper IDs or a serialized review.
It never starts a review, acquires papers, or treats novelty as verified.

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

## Connected research workspace

The web track workspace exposes **Notebook**, **Reader highlights**, **Code & artifacts**,
and **Claims & writing** without unmounting unsaved drafts when changing views.

Switching tracks restores a valid section for the destination rather than carrying an absent
exploration filename across notebooks. Import conflicts let you refresh the preview while
retaining your reviewed merge; applying it still checks the current revision.
For linked-file conflicts, compare the current file with your retained draft before explicitly
adopting its baseline. Refreshing the file inventory alone does not authorize an overwrite.
Drafts can be downloaded before reconciliation. Linked PDF previews provide selectable text
and an authorized original-file download rather than a canvas-only reading surface.

### References and manuscript

Manage the global bibliography from the library's References panel. Metadata-only records do
not download papers. Stable keys are separate from record IDs; authors, publication metadata,
local paper links and explicit related-version links remain editable. DOI/arXiv/paper/key
collisions report the existing record and never merge it silently. Imports are atomic and
revision guarded. BibTeX/RIS interchange excludes ScholarWeave-local paper/version links.
The panel documents its supported syntax and rejects unsupported fields rather than losing them.

The manuscript is `exploration/manuscript.md`, so existing section revision history, guarded
writes, workspace indexing, chat tools, and track ZIP round trips apply. Markdown headings
provide sections; `[@citation_key]` cites a saved reference, and `[claim:id]` connects a statement
to a track claim. The writing UI inserts these markers, previews the document, compares an
externally changed revision without throwing away the draft, and exports saved text.
Markdown/LaTeX/DOCX exports append cited bibliography entries and recorded evidence. Missing keys
or claim IDs block export. DOCX and LaTeX preserve draft text, not arbitrary Markdown formatting
or a journal citation style. Citation/paragraph warnings are structural checks, not a model
judgment of novelty, validity, or scientific support.

### Claim evidence

Claims live in guarded `local_data/track_claims/<track-id>.json` records, independent of
generated summaries. Each claim separates its statement, researcher interpretation, and
supporting/contradicting/contextual evidence. The service verifies each quotation against its
selected extracted page, ignoring whitespace, and records the document ID, page, title,
source version, and page-text SHA-256. The stored quotation survives source edits; reads
label unavailable or changed source evidence rather than silently recertifying it.
Claims without evidence may represent hypotheses and are explicitly flagged.

`read_research_track` actions `claims` and `references` return paginated evidence records and
real bibliography keys for writing assistance. They do not automatically read linked code.
Track ZIPs contain the manuscript Markdown, not the global bibliography or claim registry;
use a writing export when sharing a self-contained draft with its evidence appendix.

### Linked code and experiment artifacts

Connect an existing local checkout by supplying its absolute path and explicitly authorizing
access to that root. ScholarWeave never clones, executes, installs dependencies, launches a
terminal, or invokes a coding agent. Browse selected paths rather than indexing the entire
repository. Hidden files, credentials, dependency directories, symbolic links and junctions
are excluded; traversal outside the authorized root is rejected. These controls are not an
OS sandbox against a hostile process mutating the filesystem concurrently.

File previews are capped at 10 MiB, manual UTF-8 edits at 256 KiB, and directory listings at
300 entries with an explicit truncation notice. PDF pages render through pdf.js, without
executing embedded scripts; Markdown, raster images, CSV tables and JSON have in-app previews.
Version 4 notebooks display saved source cells and text/PNG outputs only, capped at 200 cells;
widgets, HTML and JavaScript outputs never run, and unsupported representations are labeled.
Unsupported binary formats show metadata rather than pretending to read their contents.
Manual UTF-8 edits require the hash of the opened file. An external edit
causes a conflict instead of an overwrite; the draft remains visible. Artifact receipts
associate a file path and hash with an existing saved experiment ID, observation time,
optional user-supplied producing revision, and a note. Refresh explicitly to detect changed
or missing artifacts. The repository's observed current commit does not establish which
revision produced an output or whether the working tree was clean. Unlinking removes the
connection/receipts, never repository files. Repository contents are not automatically sent
to any model provider.

Receipts can be opened again or explicitly promoted into Results with a researcher-supplied
observation. Promotion checks both the current artifact hash and the loaded Results revision.
It appends provenance and an unverified-observation label, never infers a scientific result
or copies an entire report into model context. Existing unsaved Results drafts are retained
and must be reconciled before saving over the promoted content. There is no automatic artifact
snapshot: retain externally generated outputs when you need the original bytes later.

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

### Topic-led research

New web reviews show three steps: ask and confirm, discover and choose, then read and compare.
Start with a question and optionally use **Add papers from arXiv** to search titles, authors or
keywords directly and add up to 24 explicit inputs. Search results and inputs are metadata only:
no PDF is downloaded until you approve a reading batch. Inputs count toward the initial
24-paper discovery limit. Confirm the proposed scope and work limits with **Find the landscape**.
Setup uses your default chat and embedding models, with explicit selectors when needed.
The research model also handles paper briefs. The embedding
model groups titles and abstracts in batches of 16.
The main model names the resulting groups; topic descriptions are metadata-based, not proof of
scientific relevance or quality.

Use **Explain fit and recommend reading** on a topic for metadata-based guidance.
Select individual paper checkboxes, enter an optional reading question, and choose **Read selected
papers**. **Find more topics** authorizes up to 100 additional discoveries, not PDF downloads.
At the selection step, **Add more papers from arXiv** searches directly and adds individual results
to the existing shortlist. Topics are updated without discarding earlier groups or authorizing reading.

New web tasks read up to six question-matched source passages per paper, not the whole paper.
Briefs appear progressively, with five sections and verbatim-checked page quotations.
The report synthesizes those partial briefs and discloses missing coverage; it is not a full-paper
audit. **Summarize paper** and **Ask a question about this paper** open contextual chat for collected
papers. **Understanding** shows coverage and **Refine understanding** compares agreements,
disagreements and open questions. **Choose follow-up reading** returns to selection without
discarding prior work. Reference discovery does not enlarge your set.

In the paper library, **Add paper** also offers direct arXiv search. **Use PDF link** fills the
download form; **Download PDF** is a separate explicit action. The reader toolbar exposes
**Summarize paper** (quick chat explanation or saved summary tools) and **Ask a question**.

Files live together in `workspace/research/<track>/<review>/`, including topic data, selections,
PDF copies, extracted text, paper summaries, model receipts, scratch notes and the final report.
Summary prose stays on disk, including after workspace-index refresh; SQLite only retains
operational metadata and file pointers. Your track receives a link to the report and the selected
paper membership. Existing library papers are reused and retained.

The default view focuses on your question, topics, reading progress and a status-specific next step.
**Activity, errors and controls** exposes pause, retry and additional-work approval when needed.
Older full-reading tasks remain read-only history in this web workspace.

### Investigating within the same workspace

Start with one research question. The same view then shows paper decisions, the work plan,
delegated progress, findings and one follow-up composer. There is no separate **Chat research tasks**
destination, assistant panel or second start action. **Saved research** groups linked papers and
investigations in one history. Evidence comparison prepares a draft in the same composer; the
Ideas view changes its focus instead of opening another chat. New paper research is linked to the
same durable investigation conversation. **Pause research** cancels the active agent run and
pauses paper work, reporting any partial failure. **Stop and summarize findings** pauses paper work
before requesting an answer from the available evidence. Resume is explicit.
Follow-ups include the selected review's question, reading focus, evidence paths and approval state;
they do not approve paper reading or replace existing evidence. Old investigation links and history
remain available, and navigating away does not stop work. **Ask** remains the contextual entry
point for questions about a particular paper.

### Legacy source-audited reviews

Saved reviews and API/terminal requests without an embedding role retain the workflow below.

The workflow below describes legacy API/terminal reviews, not the new web setup. Full-reading
records remain read-only history in the web workspace. Current coordinator proposals through
`literature_review` use topic-led targeted reading; ordinary chat cannot start staged reviews.
Scope, constraints and paper/comparison limits require your initial approval. Highly relevant and
relevant papers are then collected and read automatically.
Edit the proposed paper/comparison limits before approving the plan or extending its budget;
unchanged background status refreshes preserve those edits until you submit them.
The progress panel separates PDFs collected/reused, prepared source text, completed page decisions,
and papers selectively read and summarized. Its denominator is the selected paper set, not the
approved allowance; admitting a paper against that allowance does not mean it has been read.
Current collection/reading operations and source errors remain visible. Expand an individual
paper to load its page decisions, findings, and summaries into the view.

Paper progress refreshes are single-flight, so slow responses do not accumulate on each live
event. The backend shares one job snapshot across the paper list and does not load extracted
manifests for papers with no reading receipts.

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

The optional **separate compact summary model** changes only the paper-reading/summary work.
Select it in review setup or in the always-visible **Summary model for unfinished papers** section.
For running work, use **Pause to choose summary model**, choose the model, then resume from reading.
Existing summaries and exploration receipts are retained.
Without this role, the existing main-model grouping, reading, combination and summary-audit
workflow remains unchanged. Terminal `/review` opens the browser for model configuration and
retains the saved role when resuming.

The compact model only needs plain chat generation, not structured JSON or tools. It receives all selected
source text in one fresh, tool-free request when it fits. Four supported sections use approximate
sentence targets: motivation/contribution 5--8, method 10--15, results about 15, open questions 3--6.
These guide detail without hard word/sentence limits or padding unsupported content.
The output is plain Markdown prose and limitations: no JSON schema, claims, quotations, claim indexes, or citation
bookkeeping. Source packet pages and versions are recorded by the application, not reconstructed
by the model. There is no enhancement pass during summarization. Context is the smaller of the
model's configured context and 128K (131072). Twenty percent is reserved for output, with a default
generation cap of 26214 tokens (512--26214), including reasoning; smaller contexts lower that ceiling.
Prompt overhead and the existing safety margin also reduce input. Select low/no reasoning when supported.
**Parallel summaries** selects 1--32 same-model paper requests, default 1. Missing page decisions
finish before the parallel group starts; packets within each paper stay sequential and assemble locally.
Every child is cancelled/awaited/accounted on pause or failure, retaining completed results.
Server slots and context must support each request independently, not divide one 128K window.
Existing reviews keep their saved cap; change it to 26214 and choose parallelism on resume.

Oversized inputs split into bounded packets with no text omitted and an explicit notice;
packet summaries are combined locally rather than through more model passes. Output is stored
verbatim with no format/content checks or validation retries; headings and sentence targets are
guidance only. Summary prose is an unverified lead, not a direct quotation or verified
evidence. Previously generated prose rejected for quote mismatches or invalid claim indexes is
reusable, including after pause/resume.
Original extracted text, page coverage and source versions remain available. When analysis begins,
the main model chooses useful papers/pages from the summaries and reads their original transcripts
for source-backed evidence. It does not enhance every summary or repeat exploration. These reads
remain context-bounded, cached and within the approved corpus; cross-paper synthesis still audits
original evidence. Compact results are saved in review
receipts, never promoted to canonical full-paper summaries automatically.

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
Other generation and model switches remain exclusive; explicit same-model summary groups can
share the lane up to their review's selected parallelism. Screening and page triage are grouped by model
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
Known-ID metadata requests use the same arXiv API and are not exempt from its
[one-request-per-three-seconds policy](https://info.arxiv.org/help/api/tou.html).
Direct PDF retrieval is not treated as an unlimited-throughput exemption; arXiv's
[automated-download guidance](https://info.arxiv.org/help/robots.html) still applies.
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
