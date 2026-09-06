Create a technically precise, self-contained summary of the supplied paper, in this single model job.
If the instruction already contains the complete short-paper extraction, draft directly from it:
do not inspect or reread; the final save checkpoints that evidence and its full coverage.
Otherwise inspect once. Prepare only if extraction is unreadable. Inspection reports reusable
source-versioned evidence: read it in bounded slices when available, then resume its exact cursor.
An empty checkpoint needs no read. Do not blindly repeat prior source reads after compaction.

Use adaptive character-budgeted batches with `read_paper_summary_batch`, always serially.
There is no arbitrary page or batch cap. Follow both `next_start` and `next_offset` exactly;
a long page/chunk can span calls. Do not overlap ranges or skip their remaining characters.
After each batch, immediately append compact structured evidence, usually 100-300 words:
claims, mechanisms, exact numbers and comparison conditions, citations, limitations, unresolved
questions, and unread coverage. Preserve cross-boundary questions for the next batch rather than
rereading an overlap. Do not copy raw text into checkpoints. A verified checkpoint makes prior raw
text discardable; it is durable evidence, not merely a run artifact or a replacement for user notes.
Continue until `has_more` is false. If the actual run/turn budget is low, reserve time to save an
explicitly partial summary that states unread coverage; never claim the first 50 pages suffice.

The final checkpoint append returns `final_checkpoint` when it fits; draft from that field without a
separate checkpoint read. Only reread the checkpoint in bounded slices when resuming or when
`final_checkpoint` is unavailable. Synthesize from that durable evidence record within this single
agent job. Preserve exact numbers, units, comparison conditions, and page or chunk citations. Every
material factual claim should carry a valid paper citation. Mark information as not reported instead
of inferring it, distinguish the authors' claims from your interpretation, and identify material
evidence gaps.

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
and any remaining evidence limits. Do not delegate or run parallel model workers. Evidence records
remain reusable across runs only for the same source/extraction version; they never overwrite notes.
