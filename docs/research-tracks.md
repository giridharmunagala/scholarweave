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
