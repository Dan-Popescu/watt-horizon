"""Conservative scalar promotion checks over retained contextual observations."""

import re
from collections import defaultdict
from collections.abc import Sequence

from radar.domain.quantity_context import QuantityObservation
from radar.domain.text import contains_quote, fold
from radar.extraction.facts import Facts


def quantity_issues(facts: Facts, observations: Sequence[QuantityObservation]) -> tuple[str, ...]:
    """Return evidence of ambiguity, never choose a phase, project, scenario or equipment total."""
    issues: set[str] = set()
    grouped: dict[tuple[object, ...], set[tuple[float, float]]] = defaultdict(set)
    projects: set[str] = set()
    for observation in observations:
        if observation.eligible:
            grouped[
                (observation.project, observation.asset, observation.state, observation.dimension, observation.basis)
            ].add((observation.low, observation.high))
            if observation.project:
                projects.add(observation.project)
    if any(len(values) > 1 for values in grouped.values()):
        issues.add("contradictory_project_quantities")
    if len(projects) > 1 or "multiple" in projects:
        issues.add("multiple_named_projects")
    evidence = facts.evidence.get("power")
    if evidence is not None:
        for observation in observations:
            if observation.document_id != evidence.document_id:
                continue
            if not (
                contains_quote(observation.quote, evidence.quote) or contains_quote(evidence.quote, observation.quote)
            ):
                continue
            if observation.phase or observation.scenario:
                issues.add("phase_or_scenario_requires_selection")
            if observation.asset in {"solar", "equipment", "connection", "mixed"}:
                issues.add("quantity_belongs_to_another_asset")
            if observation.state in {"existing", "example"}:
                issues.add("quantity_is_not_proposed")
            contextual_text = fold(observation.quote + " " + observation.header_quote)
            if observation.state == "unknown" and re.search(r"existant\w*|actuel\w*|ancien\w*", contextual_text):
                issues.add("proposed_and_existing_not_separated")
            if observation.basis in {"dc", "c"} or observation.uncertain_reason:
                issues.add("quantity_basis_or_document_uncertain")
    return tuple(sorted(issues))
