from __future__ import annotations

from backend.schemas import WorkflowDefinition

_DOCUMENT_INPUT = {
    "id": "document",
    "type": "workflow_input",
    "config": {
        "key": "document_id",
        "kind": "text",
        "label": "Paper",
        "description": "The uploaded document to work on.",
    },
}

_PREPARE_CHUNKS = '''def transform(inputs):
    """Turns stored chunks into readable passages the summariser can group and read.

    Inputs are named after the port they came from, so the wired "chunks" output of the
    document step arrives as inputs["chunks"]. Whatever this returns becomes the node's
    value output — here a list, so the map step below can walk it.
    """
    passages = []
    for chunk in inputs.get("chunks") or []:
        heading = chunk.get("section_title") or f"Chunk {chunk.get('chunk_index', 0) + 1}"
        citation = chunk.get("citation") or ""
        passages.append(f"## {heading} ({citation})\\n{chunk.get('text', '')}")
    return passages
'''

_PAPER_NAMING_PROMPT = '''def transform(inputs):
    """Builds a tiny prompt — title plus opening text — for the short folder name.

    Only the first few chunks are used, so the naming call stays cheap enough for the
    small default model even when the paper is long.
    """
    document = inputs.get("document") or {}
    title = str(document.get("title") or "").strip() or "Untitled paper"
    opening = []
    used = 0
    for chunk in (inputs.get("chunks") or [])[:6]:
        text = str(chunk.get("text", "")).strip()
        if not text:
            continue
        opening.append(text)
        used += len(text)
        if used > 1200:
            break
    abstract = " ".join(opening)[:1200]
    return "Paper title: " + title + "\\n\\nOpening text (usually the abstract):\\n" + abstract
'''

_COUNT_TERMS = '''def transform(inputs):
    """Counts term occurrences so the agent can check emphasis without guessing."""
    text = str(inputs.get("text", "")).lower()
    terms = inputs.get("terms") or []
    if isinstance(terms, str):
        terms = [part.strip() for part in terms.split(",") if part.strip()]
    counts = {term: text.count(str(term).lower()) for term in terms}
    return {"counts": counts, "total_words": len(text.split())}
'''


STARTER_WORKFLOWS = [
    WorkflowDefinition.model_validate(
        {
            "name": "Paper ingestion",
            "description": "Reads a PDF, stores its text and citations, and indexes it for retrieval. No model calls.",
            "nodes": [
                _DOCUMENT_INPUT,
                {"id": "ingest", "type": "pdf_ingest"},
                {"id": "index", "type": "index_chunks"},
                {"id": "output", "type": "final_output", "config": {"artifact_name": "ingestion-result", "format": "json"}},
            ],
            "edges": [
                {"source_node_id": "document", "source_port": "text", "target_node_id": "ingest", "target_port": "document_id"},
                {"source_node_id": "document", "source_port": "text", "target_node_id": "index", "target_port": "document_id"},
                {"source_node_id": "ingest", "source_port": "document", "target_node_id": "output", "target_port": "content"},
            ],
        }
    ),
    WorkflowDefinition.model_validate(
        {
            "name": "Enhance OCR page",
            "description": "Rebuilds one badly scanned page with a vision model, keeping the OCR text as a fallback.",
            "nodes": [
                _DOCUMENT_INPUT,
                {
                    "id": "page",
                    "type": "workflow_input",
                    "config": {
                        "key": "page_number",
                        "kind": "number",
                        "label": "Page number",
                        "description": "1-based page to rewrite.",
                    },
                },
                {"id": "enhance", "type": "enhance_document_page"},
                {"id": "output", "type": "final_output", "config": {"artifact_name": "page-enhancement", "format": "json"}},
            ],
            "edges": [
                {"source_node_id": "document", "source_port": "text", "target_node_id": "enhance", "target_port": "document_id"},
                {"source_node_id": "page", "source_port": "value", "target_node_id": "enhance", "target_port": "page_number"},
                {"source_node_id": "enhance", "source_port": "page", "target_node_id": "output", "target_port": "content"},
            ],
        }
    ),
    WorkflowDefinition.model_validate(
        {
            "name": "Hierarchical summary",
            "description": (
                "Summarises a paper by folding it up in three passes: batches of sections become notes, "
                "notes are merged into cluster summaries, and a final agent writes the briefing. No single "
                "prompt ever receives the whole paper, so this works on long papers. The briefing and every "
                "section note are also saved to Notes, in one folder named after the paper."
            ),
            "nodes": [
                _DOCUMENT_INPUT,
                {"id": "load", "type": "select_document"},
                {"id": "prepare", "type": "python_code", "config": {"code": _PREPARE_CHUNKS, "entrypoint": "transform"}},
                {
                    "id": "sections",
                    "type": "map_subflow",
                    "config": {
                        "item_key": "passages",
                        "group_size": 8,
                        "concurrency": 3,
                        "max_items": 1024,
                        "on_overflow": "truncate",
                        "subflow": {
                            "name": "Section notes",
                            "nodes": [
                                {"id": "passages", "type": "text_input", "config": {"input_key": "passages"}},
                                {
                                    "id": "summariser",
                                    "type": "agent",
                                    "config": {
                                        "name": "Section summariser",
                                        "instructions": (
                                            "You summarise part of a research paper.\n\n"
                                            "Write at most five bullets covering the claims, methods and numbers in "
                                            "the passages below. Keep every citation marker exactly as written, and "
                                            "say nothing the passages do not support.\n\n"
                                            "Reply with the bullets only — no preamble, no heading, no closing "
                                            "remark. If the passages are only references or author lists, reply "
                                            "with the single word SKIP.\n\n"
                                            "Passages:\n{{input}}"
                                        ),
                                    },
                                },
                                {"id": "final", "type": "final_output"},
                            ],
                            "edges": [
                                {
                                    "source_node_id": "passages",
                                    "source_port": "text",
                                    "target_node_id": "summariser",
                                    "target_port": "input",
                                },
                                {
                                    "source_node_id": "summariser",
                                    "source_port": "text",
                                    "target_node_id": "final",
                                    "target_port": "content",
                                },
                            ],
                        },
                    },
                },
                {
                    "id": "clusters",
                    "type": "map_subflow",
                    "config": {
                        "item_key": "notes",
                        "group_size": 4,
                        "concurrency": 3,
                        "max_items": 1024,
                        "on_overflow": "truncate",
                        "subflow": {
                            "name": "Cluster summary",
                            "nodes": [
                                {"id": "notes", "type": "text_input", "config": {"input_key": "notes"}},
                                {
                                    "id": "merger",
                                    "type": "agent",
                                    "config": {
                                        "name": "Note merger",
                                        "instructions": (
                                            "You merge notes taken from consecutive parts of one research "
                                            "paper.\n\n"
                                            "Combine the notes below into at most eight bullets, dropping "
                                            "repetition and anything marked SKIP. Keep the citation markers and "
                                            "the numbers exactly as written, and add nothing of your own. Reply "
                                            "with the bullets only.\n\n"
                                            "Notes:\n{{input}}"
                                        ),
                                    },
                                },
                                {"id": "final", "type": "final_output"},
                            ],
                            "edges": [
                                {
                                    "source_node_id": "notes",
                                    "source_port": "text",
                                    "target_node_id": "merger",
                                    "target_port": "input",
                                },
                                {
                                    "source_node_id": "merger",
                                    "source_port": "text",
                                    "target_node_id": "final",
                                    "target_port": "content",
                                },
                            ],
                        },
                    },
                },
                {"id": "notes", "type": "reduce_combine", "config": {"mode": "join_text", "join_with": "\n\n"}},
                {
                    "id": "synthesis",
                    "type": "agent",
                    "config": {
                        "name": "Briefing writer",
                        "instructions": (
                            "You write the final briefing for a research paper from section notes.\n\n"
                            "Produce Markdown under these headings: Summary, Contributions, Method, Results, "
                            "Limitations. Merge repetition across the notes, keep the citation markers, and do not "
                            "introduce anything the notes do not contain. Ignore bibliography entries, author "
                            "lists and acknowledgements — describe what the paper argues and shows, never its "
                            "reference list.\n\n"
                            "Section notes:\n{{input}}"
                        ),
                    },
                },
                {"id": "markdown", "type": "markdown"},
                {
                    "id": "naming_prompt",
                    "type": "python_code",
                    "config": {"code": _PAPER_NAMING_PROMPT, "entrypoint": "transform"},
                },
                {
                    "id": "namer",
                    "type": "agent",
                    "config": {
                        # No model is pinned, so this uses the default generation model —
                        # the small local one is plenty for naming a folder.
                        "name": "Paper namer",
                        "temperature": 0.0,
                        "instructions": (
                            "You name folders in a research notes app.\n\n"
                            "Read the paper title and abstract below, then reply with a short name "
                            "for the paper: two to five plain words separated by spaces. No "
                            "punctuation, no quotes, no explanation, no preamble — the name only.\n\n"
                            "{{input}}"
                        ),
                    },
                },
                {
                    "id": "notes_folder",
                    "type": "write_note_folder",
                    "config": {
                        "summary_name": "summary",
                        "item_prefix": "section",
                        "item_heading": "Section notes",
                    },
                },
                {"id": "output", "type": "final_output", "config": {"artifact_name": "summary", "format": "md"}},
            ],
            "edges": [
                {"source_node_id": "document", "source_port": "text", "target_node_id": "load", "target_port": "document_id"},
                {"source_node_id": "load", "source_port": "chunks", "target_node_id": "prepare", "target_port": "inputs"},
                {"source_node_id": "prepare", "source_port": "value", "target_node_id": "sections", "target_port": "items"},
                {"source_node_id": "sections", "source_port": "results", "target_node_id": "clusters", "target_port": "items"},
                {"source_node_id": "clusters", "source_port": "results", "target_node_id": "notes", "target_port": "items"},
                {"source_node_id": "notes", "source_port": "text", "target_node_id": "synthesis", "target_port": "input"},
                {"source_node_id": "synthesis", "source_port": "text", "target_node_id": "markdown", "target_port": "value"},
                {"source_node_id": "markdown", "source_port": "markdown", "target_node_id": "output", "target_port": "content"},
                {"source_node_id": "load", "source_port": "document", "target_node_id": "naming_prompt", "target_port": "inputs"},
                {"source_node_id": "load", "source_port": "chunks", "target_node_id": "naming_prompt", "target_port": "inputs"},
                {"source_node_id": "naming_prompt", "source_port": "text", "target_node_id": "namer", "target_port": "input"},
                {"source_node_id": "namer", "source_port": "text", "target_node_id": "notes_folder", "target_port": "folder"},
                {"source_node_id": "load", "source_port": "document", "target_node_id": "notes_folder", "target_port": "title"},
                {"source_node_id": "sections", "source_port": "results", "target_node_id": "notes_folder", "target_port": "items"},
                {"source_node_id": "markdown", "source_port": "markdown", "target_node_id": "notes_folder", "target_port": "summary"},
            ],
        }
    ),
    WorkflowDefinition.model_validate(
        {
            "name": "Paper Q&A",
            "description": (
                "An agent answers a question by searching the paper itself, deciding how many searches to "
                "run and which passages to quote."
            ),
            "nodes": [
                _DOCUMENT_INPUT,
                {
                    "id": "question",
                    "type": "workflow_input",
                    "config": {
                        "key": "question",
                        "kind": "text",
                        "label": "Question",
                        "description": "What to ask about the paper.",
                    },
                },
                {"id": "search", "type": "vector_retrieve", "config": {"top_k": 6}},
                {"id": "grep", "type": "keyword_retrieve", "config": {"top_k": 6}},
                {
                    "id": "answer",
                    "type": "agent",
                    "config": {
                        "name": "Paper analyst",
                        "instructions": (
                            "You answer questions about one research paper.\n\n"
                            "Search the paper before answering: use search_paper for ideas and "
                            "search_paper_keywords for exact terms, names or numbers. Answer only from what the "
                            "searches return, quote the citation markers you relied on, and say plainly when the "
                            "paper does not cover something.\n\n"
                            "Question: {{input}}"
                        ),
                        "max_turns": 8,
                    },
                },
                {"id": "output", "type": "final_output", "config": {"artifact_name": "qa-answer", "format": "md"}},
            ],
            "edges": [
                {"source_node_id": "document", "source_port": "text", "target_node_id": "search", "target_port": "document_id"},
                {"source_node_id": "document", "source_port": "text", "target_node_id": "grep", "target_port": "document_id"},
                {"source_node_id": "search", "source_port": "tool", "target_node_id": "answer", "target_port": "tools"},
                {"source_node_id": "grep", "source_port": "tool", "target_node_id": "answer", "target_port": "tools"},
                {"source_node_id": "question", "source_port": "text", "target_node_id": "answer", "target_port": "input"},
                {"source_node_id": "answer", "source_port": "text", "target_node_id": "output", "target_port": "content"},
            ],
        }
    ),
    WorkflowDefinition.model_validate(
        {
            "name": "Research agent with handoff",
            "description": (
                "A researcher agent gathers evidence with search and a Python step, then hands over to a "
                "writer agent. Shows tools, custom code and handoffs together."
            ),
            "nodes": [
                _DOCUMENT_INPUT,
                {
                    "id": "brief",
                    "type": "workflow_input",
                    "config": {
                        "key": "brief",
                        "kind": "text",
                        "label": "Brief",
                        "description": "What the write-up should cover.",
                    },
                },
                {"id": "search", "type": "vector_retrieve", "config": {"top_k": 8}},
                {
                    "id": "stats",
                    "type": "python_code",
                    "config": {
                        "code": _COUNT_TERMS,
                        "tool_name": "count_terms",
                        "tool_description": 'Counts how often each term appears in a passage. Pass {"text": ..., "terms": [...]}.',
                    },
                },
                {
                    "id": "writer",
                    "type": "agent",
                    "config": {
                        "name": "Writer",
                        "handoff_description": "Hand over once the evidence is gathered, to turn notes into prose.",
                        "instructions": (
                            "You turn research notes into a short, readable write-up in Markdown.\n\n"
                            "Keep every citation marker, prefer plain words over jargon, and add no claim the "
                            "notes do not support."
                        ),
                    },
                },
                {
                    "id": "researcher",
                    "type": "agent",
                    "config": {
                        "name": "Researcher",
                        "instructions": (
                            "You gather evidence from one research paper.\n\n"
                            "Search the paper for each part of the brief, and use count_terms when you want to "
                            "check how much attention the paper gives a term. Once you have enough evidence, hand "
                            "off to the Writer with your notes and citations.\n\n"
                            "Brief: {{input}}"
                        ),
                        "max_turns": 12,
                    },
                },
                {"id": "output", "type": "final_output", "config": {"artifact_name": "research-note", "format": "md"}},
            ],
            "edges": [
                {"source_node_id": "document", "source_port": "text", "target_node_id": "search", "target_port": "document_id"},
                {"source_node_id": "search", "source_port": "tool", "target_node_id": "researcher", "target_port": "tools"},
                {"source_node_id": "stats", "source_port": "tool", "target_node_id": "researcher", "target_port": "tools"},
                {"source_node_id": "writer", "source_port": "agent", "target_node_id": "researcher", "target_port": "handoffs"},
                {"source_node_id": "brief", "source_port": "text", "target_node_id": "researcher", "target_port": "input"},
                {"source_node_id": "researcher", "source_port": "text", "target_node_id": "output", "target_port": "content"},
            ],
        }
    ),
]
