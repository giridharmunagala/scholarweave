"""Run the research agent against the configured default Ollama profile.

Usage: ``python scripts/smoke_agent.py [model]``
"""

from __future__ import annotations

import asyncio
import sys

from backend.conversations.turns import research_blueprint
from backend.bootstrap import create_services
from backend.core.config import Settings


AGENT_NAME = "ScholarWeave research"
MODEL = sys.argv[1] if len(sys.argv) > 1 else None


async def main() -> None:
    services = create_services(Settings())
    try:
        if MODEL:
            default_profile = next(
                profile
                for profile in services.model_runtime.profiles()
                if profile.name == "Default Ollama"
            )
            reference = {
                "provider_profile_id": default_profile.id,
                "model": MODEL,
            }
            services.settings.default_model_references = {
                **services.settings.default_model_references,
                "chat": reference,
                "tools": reference,
            }

        blueprint = research_blueprint(
            services.settings.default_model_references.get("chat", {})
        )
        compiled = services.compiler.compile(blueprint)
        run = await services.runs.run_now(
            compiled,
            "What problem does the most relevant paper set out to solve?",
        )

        print(f"agent: {AGENT_NAME}")
        print(f"status: {run.status}")
        if run.error:
            print(f"error: {run.error}")
        else:
            print(f"output:\n{run.final_output_json}")
    finally:
        await services.close()


asyncio.run(main())
