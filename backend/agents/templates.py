from __future__ import annotations

from backend.agents.blueprint import AgentBlueprint


def starter_blueprints() -> list[AgentBlueprint]:
    return [
        AgentBlueprint.model_validate(
            {
                "name": "Paper research assistant",
                "description": "Searches local papers and writes cited research artifacts.",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Paper Researcher",
                        "instructions": (
                            "Search the local paper library for the user's question. Cite every "
                            "claim using the chunk citation returned by the tools. Write an artifact "
                            "only when the user asks for a durable report."
                        ),
                        "tool_ids": [
                            "list-papers",
                            "read-chunks",
                            "search-papers",
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
                ],
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
                        "instructions": "Search papers, synthesize evidence, and preserve citations.",
                        "tool_ids": ["search-papers", "read-chunks"],
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
            }
        ),
    ]
