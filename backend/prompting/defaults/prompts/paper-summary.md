Create a technically precise, self-contained summary of the one supplied paper. Work independently
from the calling agent and use the same model selected for that agent. Begin by reading the
run-scoped checkpoint from offset 0; an empty checkpoint means this is a fresh job, while existing
content must be reused after an interruption. If a ScholarWeave context-compaction checkpoint
appears during the job, reread this paper-summary checkpoint before continuing. Inspect the paper.
If its extracted text is unreadable, prepare it once.

Read the paper in at most five batches with `read_paper_summary_batch`. Never issue batch reads in
parallel. The runtime permits exactly one outstanding batch and rejects another batch until the
current batch has been durably checkpointed. Every pages call returns up
to 10 pages; use pages 1-50 in order and stop after the fifth batch even when the paper is longer.
If page reads are unavailable, use at most five 10-chunk batches instead. After each batch,
immediately append a substantial but bounded understanding record of roughly 800-1500 words. Include
the covered page/chunk range, the argument and mechanisms, material findings, exact numbers and
comparison conditions, citations, limitations, unresolved questions, and what remains unread. Do
not copy raw paper text or draft final prose into the checkpoint. Once the checkpoint append succeeds and reports the verified checkpoint path, the next model turn
removes that batch's raw paper, retained-result output, checkpoint-read output, and appended content
from context, leaving only a compact checkpoint path and coverage receipt. For papers of 50 pages or fewer, continue until
`has_more` is false; for longer papers, treat the first 50 pages as sufficient and do not read the
remainder. Follow a truncated result's `result_ref` with `read_tool_result` until that 10-page batch
is understood before checkpointing it.

Before drafting, reread the complete checkpoint in bounded slices by following `has_more` and
`next_offset`. Synthesize from that durable evidence record within this single agent job. Preserve
exact numbers, units, comparison conditions, and page or chunk citations. Every material factual
claim should carry a valid paper citation. Mark information as not reported instead of inferring it,
distinguish the authors' claims from your interpretation, and identify material evidence gaps.

Use exactly four sections:

1. **Motivation, contribution, and novelty** - at most one prose paragraph.
2. **Method** - two or three prose paragraphs.
3. **Results** - at most two prose paragraphs, plus compact Markdown tables where useful for
   datasets, baselines, quantitative comparisons, and ablations. Include limitations here.
4. **Open questions** - at most one prose paragraph.

Before saving, self-review the complete draft for contribution fidelity, method completeness,
assumptions, experimental coverage, numerical accuracy, citation support, limitations, uncertainty,
and unsupported extrapolation. Correct every defect you find. Call `save_paper_summary_version`
exactly once with the final Markdown and a concise `review_summary` describing the checks performed
and any remaining evidence limits. Do not delegate any portion of the summary. The checkpoint is
temporary run state, not a user-facing summary or category-wise partial summary.
