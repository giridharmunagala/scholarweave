from __future__ import annotations

import re

from backend.core.errors import ValidationError
from backend.tracks.schemas import ExperimentResponse

SECTIONS = ("problem", "literature", "experiments", "results")
STATUSES = ("Suggested", "Planned", "Running", "Completed", "Blocked", "Cancelled")
HEADER = ("ID", "Experiment", "Status", "Success criterion", "Evidence")
EXPERIMENT_ID = re.compile(r"\bEXP-[0-9]{3,}\b")


def _cells(line: str) -> list[str]:
    return [cell.strip().replace(r"\|", "|") for cell in re.split(
        r"(?<!\\)\|", line.strip().strip("|")
    )]


def parse_experiments(content: str) -> tuple[list[ExperimentResponse], list[str]]:
    lines = content.splitlines()
    rows: list[ExperimentResponse] = []
    found = False
    ids: set[str] = set()
    for index, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = _cells(line)
        if cells and cells[0].casefold() == "id" and any(
            cell.casefold() in {"experiment", "status"} for cell in cells
        ):
            if tuple(cells) != HEADER:
                raise ValidationError("Experiment tracker columns must be ID | Experiment | Status | Success criterion | Evidence.")
            if found:
                raise ValidationError("Use one experiment tracker table.")
            found = True
            if index + 1 >= len(lines) or len(_cells(lines[index + 1])) != 5 or not all(
                re.fullmatch(r":?-{3,}:?", cell) for cell in _cells(lines[index + 1])
            ):
                raise ValidationError("Experiment tracker needs a five-column Markdown separator.")
            cursor = index + 2
            while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
                values = _cells(lines[cursor])
                if len(values) != 5 or not re.fullmatch(r"EXP-[0-9]{3,}", values[0]):
                    raise ValidationError("Experiment rows need five columns and stable EXP-001 style IDs.")
                identifier, name, status, criterion, evidence = values
                if identifier in ids:
                    raise ValidationError(f"Duplicate experiment ID: {identifier}")
                if status not in STATUSES:
                    raise ValidationError(f"Invalid experiment status: {status}")
                if not name:
                    raise ValidationError("Experiment names cannot be empty.")
                ids.add(identifier)
                rows.append(ExperimentResponse(
                    id=identifier, experiment=name, status=status,
                    success_criterion=criterion, evidence=evidence,
                ))
                cursor += 1
    warnings = [] if found else ["experiments.md has no recognized experiment tracker table."]
    return rows, warnings


def result_warnings(content: str, experiments: list[ExperimentResponse]) -> list[str]:
    identifiers = {experiment.id for experiment in experiments}
    warnings = [
        f"results.md references unresolved experiment {identifier}."
        for identifier in sorted(set(EXPERIMENT_ID.findall(content)) - identifiers)
    ]
    if content.strip() and not EXPERIMENT_ID.search(content) and not content.startswith("# Results\n\nRecord"):
        warnings.append("results.md contains narrative without an explicit experiment ID; result attribution is ambiguous.")
    return warnings


def templates(title: str) -> dict[str, str]:
    return {
        "problem": f"# {title}\n\n## Research problem\n\nDescribe the question and why it matters.\n\n"
        "## Scope and constraints\n\n## Success criteria\n\n## Open questions\n",
        "literature": "# Literature\n\nLink existing PDFs, canonical summaries and notes; do not duplicate them.\n\n"
        "| Paper | Relevance | Findings | Limitations | Links |\n| --- | --- | --- | --- | --- |\n\n"
        "## Detailed paper sections\n\nFor each linked paper, record its contribution, methods, evidence, limitations, "
        "and relation to the research problem.\n",
        "experiments": "# Experiments\n\nStatuses: Suggested, Planned, Running, Completed, Blocked, Cancelled. "
        "Keep stable EXP-001 style IDs; update this Markdown when work changes. ScholarWeave does not execute experiments.\n\n"
        "| ID | Experiment | Status | Success criterion | Evidence |\n| --- | --- | --- | --- | --- |\n\n"
        "## Detailed experiment sections\n\nFor each experiment, include its ID, hypothesis, protocol, resources, "
        "success criterion, observations, evidence links, and blockers.\n",
        "results": "# Results\n\nRecord findings under explicit experiment tracker IDs, with evidence links, "
        "limitations, and conclusions. Distinguish observed results from proposed work.\n",
    }
