Grade a Markdown reconstruction of one PDF page by comparing it with the supplied page image and OCR draft.

Rate `good` when the candidate faithfully represents all readable content and structure. Rate `average` for minor formatting or wording slips. Rate `poor` when meaningful content is omitted or invented, numbers or citations change, reading order is scrambled, equations are corrupted, or tables are materially misrepresented.

Local `/api/artifacts/` image references are system-added and must not lower the rating. List concise, image-supported issues and return only the requested JSON object.
