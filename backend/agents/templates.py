from __future__ import annotations

from backend.agents.blueprint import AgentBlueprint


def starter_blueprints() -> list[AgentBlueprint]:
    return [
        AgentBlueprint.model_validate(
            {
                "name": "Paper research assistant",
                "description": "Searches local and open sources and writes cited research artifacts.",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Paper Researcher",
                        "instructions": (
                            "Complete the user's research task using local papers, arXiv, Wikipedia, "
                            "and web search as appropriate. Start with focused queries, recursively "
                            "follow useful terms and citations with narrower searches, and cross-check "
                            "important claims before answering. Stop when the available evidence is "
                            "sufficient. Cite local claims with returned chunk citations and external "
                            "claims with returned source URLs. When the user asks to download an arXiv "
                            "result, call download_paper with its pdf_url; the returned paper is already "
                            "extracted and indexed. Download HTML pages before answering questions about "
                            "their full content, and save page notes when requested. Write an artifact "
                            "only when the user asks for a durable report."
                        ),
                        "tool_ids": [
                            "list-papers",
                            "read-chunks",
                            "search-papers",
                            "search-web",
                            "search-arxiv",
                            "search-wikipedia",
                            "download-paper",
                            "download-web-page",
                            "list-web-pages",
                            "read-web-page",
                            "search-web-page",
                            "save-web-page-note",
                            "write-artifact",
                        ],
                    }
                ],
                "tools": [
                    {
                        "id": "list-papers",
                        "kind": "function",
                        "catalog_id": "documents.list",
                    },
                    {
                        "id": "read-chunks",
                        "kind": "function",
                        "catalog_id": "documents.read_chunks",
                    },
                    {
                        "id": "search-papers",
                        "kind": "function",
                        "catalog_id": "retrieval.keyword_search",
                    },
                    {
                        "id": "write-artifact",
                        "kind": "function",
                        "catalog_id": "artifacts.write",
                    },
                    {"id": "search-web", "kind": "function", "catalog_id": "web.search"},
                    {
                        "id": "search-arxiv",
                        "kind": "function",
                        "catalog_id": "arxiv.search",
                    },
                    {
                        "id": "search-wikipedia",
                        "kind": "function",
                        "catalog_id": "wikipedia.search",
                    },
                    {"id": "download-paper", "kind": "function", "catalog_id": "documents.download"},
                    {"id": "download-web-page", "kind": "function", "catalog_id": "webpage.download"},
                    {"id": "list-web-pages", "kind": "function", "catalog_id": "webpage.list"},
                    {"id": "read-web-page", "kind": "function", "catalog_id": "webpage.read"},
                    {"id": "search-web-page", "kind": "function", "catalog_id": "webpage.search"},
                    {"id": "save-web-page-note", "kind": "function", "catalog_id": "webpage.notes.save"},
                ],
                "run": {"max_turns": 30, "max_tool_concurrency": 1},
            }
        ),
        AgentBlueprint.model_validate(
            {
                "name": "Research team",
                "description": "Delegates research and review using SDK handoffs.",
                "entry_agent_id": "triage",
                "agents": [
                    {
                        "id": "triage",
                        "name": "Research Triage",
                        "instructions": (
                            "Clarify the research task, then hand off to the researcher. "
                            "Use the reviewer as a tool before delivering high-stakes conclusions."
                        ),
                    },
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": (
                            "Iteratively search local papers, arXiv, Wikipedia, and the web. Refine "
                            "queries from earlier results, cross-check claims, synthesize the evidence, "
                            "and preserve local citations and external source URLs. Download selected "
                            "arXiv PDFs for full-text analysis, and download HTML pages before answering "
                            "questions about their full content."
                        ),
                        "tool_ids": [
                            "search-papers",
                            "read-chunks",
                            "search-web",
                            "search-arxiv",
                            "search-wikipedia",
                            "download-paper",
                            "download-web-page",
                            "list-web-pages",
                            "read-web-page",
                            "search-web-page",
                            "save-web-page-note",
                        ],
                    },
                    {
                        "id": "reviewer",
                        "name": "Reviewer",
                        "instructions": "Check the supplied draft for unsupported claims and missing caveats.",
                    },
                ],
                "tools": [
                    {
                        "id": "search-papers",
                        "kind": "function",
                        "catalog_id": "retrieval.keyword_search",
                    },
                    {
                        "id": "read-chunks",
                        "kind": "function",
                        "catalog_id": "documents.read_chunks",
                    },
                    {"id": "search-web", "kind": "function", "catalog_id": "web.search"},
                    {
                        "id": "search-arxiv",
                        "kind": "function",
                        "catalog_id": "arxiv.search",
                    },
                    {
                        "id": "search-wikipedia",
                        "kind": "function",
                        "catalog_id": "wikipedia.search",
                    },
                    {"id": "download-paper", "kind": "function", "catalog_id": "documents.download"},
                    {"id": "download-web-page", "kind": "function", "catalog_id": "webpage.download"},
                    {"id": "list-web-pages", "kind": "function", "catalog_id": "webpage.list"},
                    {"id": "read-web-page", "kind": "function", "catalog_id": "webpage.read"},
                    {"id": "search-web-page", "kind": "function", "catalog_id": "webpage.search"},
                    {"id": "save-web-page-note", "kind": "function", "catalog_id": "webpage.notes.save"},
                ],
                "handoffs": [
                    {
                        "id": "delegate-research",
                        "source_agent_id": "triage",
                        "target_agent_id": "researcher",
                    }
                ],
                "agent_tools": [
                    {
                        "id": "review-tool",
                        "owner_agent_id": "triage",
                        "delegate_agent_id": "reviewer",
                        "tool_name": "review_research",
                        "tool_description": "Review a research draft for evidence quality.",
                    }
                ],
                "run": {"max_turns": 30, "max_tool_concurrency": 1},
            }
        ),
    ]
