"""Runs a starter workflow against the live database, for end-to-end checking by hand.

Usage: ``.venv/bin/python scripts/smoke_agent_workflow.py "Hierarchical summary"``
"""

from __future__ import annotations

import asyncio
import sys
import time

from sqlalchemy import select

from backend.app import create_backend_services
from backend.config import Settings
from backend.models import Document, NodeRun, Run
from backend.templates import STARTER_WORKFLOWS

WORKFLOW = sys.argv[1] if len(sys.argv) > 1 else "Hierarchical summary"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "gemma4:e2b"


async def main() -> None:
    settings = Settings()
    services = create_backend_services(settings)
    # Persisted settings are applied during service construction, so override afterwards.
    services.settings.default_generation_model = MODEL
    definition = next(w for w in STARTER_WORKFLOWS if w.name == WORKFLOW)

    with services.session_factory() as session:
        document = session.scalars(select(Document)).first()
    if document is None:
        raise SystemExit("No document in the database — ingest a paper first.")
    print(f"workflow : {WORKFLOW}\nmodel    : {settings.default_generation_model}\ndocument : {document.id}")

    inputs = {
        "document_id": document.id,
        "question": "What problem does this paper set out to solve?",
        "brief": "Summarise the paper's main claims and how they are supported.",
    }
    started = time.monotonic()
    run = await services.executor.start_run(definition, inputs)
    while True:
        await asyncio.sleep(2)
        with services.session_factory() as session:
            row = session.get(Run, run.id)
            assert row is not None
            status, error, output = row.status, row.error, row.output_json
            done = session.scalars(select(NodeRun).where(NodeRun.run_id == run.id)).all()
        finished = sum(1 for n in done if n.status == "completed")
        print(f"  {time.monotonic() - started:6.1f}s  {status:10} nodes {finished}/{len(done)}", flush=True)
        if status in {"completed", "failed", "cancelled"}:
            break

    print(f"\nstatus: {status} after {time.monotonic() - started:.1f}s")
    if error:
        print("error:", error)
        for node in done:
            if node.status == "failed":
                print(f"  failed node {node.node_path} ({node.node_type}): {node.error}")
    print("\noutput:\n", str(output)[:2000])


asyncio.run(main())
