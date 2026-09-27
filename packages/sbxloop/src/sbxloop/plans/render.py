"""A plan node as the issue it becomes on the forge (#2341).

The body is the node's sections under markdown headings — what a person
reads on the forge and what a run's decompose reads back — followed by the
hidden marker ``<!-- sbx-plan: <plan_id>/<node_id> -->``. The marker is how
publishing stays idempotent: an issue an interrupted attempt already
created is found by it, never created twice. Everything here is pure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from sbxloop.plans.model import ForgeRef, PlanNode

_MARKER = re.compile(r"<!--\s*sbx-plan:\s*(?P<plan>[\w-]+)/(?P<node>[\w-]+)\s*-->")


def marker(plan_id: str, node_id: str) -> str:
    """The hidden line that ties an issue to its plan node."""
    return f"<!-- sbx-plan: {plan_id}/{node_id} -->"


def markers(body: str) -> list[tuple[str, str]]:
    """Every ``(plan_id, node_id)`` marker ``body`` carries, in order."""
    return [(m.group("plan"), m.group("node")) for m in _MARKER.finditer(body)]


def marked(body: str, plan_id: str, node_id: str) -> bool:
    """Whether ``body`` carries the marker of ``plan_id``/``node_id``."""
    return (plan_id, node_id) in markers(body)


def issue_reference(from_repo: str, to_repo: str, number: int) -> str:
    """How an issue in ``from_repo`` names issue ``number`` of ``to_repo``:
    ``#12`` in the same repository, ``group/project#12`` across them (both
    forges read that form)."""
    if from_repo.casefold() == to_repo.casefold():
        return f"#{number}"
    return f"{to_repo}#{number}"


def _fence(lines: list[str]) -> str:
    """A code block no line inside can close early."""
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    fence = "`" * max(3, longest + 1)
    return "\n".join([fence, *lines, fence])


def render_body(
    node: PlanNode,
    *,
    dependencies: Mapping[str, tuple[str, ForgeRef]] | None = None,
) -> str:
    """The issue body for ``node``: its non-empty sections, then the marker.

    ``dependencies`` maps each ``depends_on`` sibling id to its repository
    and forge reference, rendered as issue references; a dependency not in
    it (never the case once its sibling is published) is named by id.
    """
    parts: list[str] = []

    def section(heading: str, text: str) -> None:
        text = text.strip()
        if text:
            parts.append(f"## {heading}\n\n{text}")

    section("Goal", node.goal)
    section("Context", node.context)
    criteria = [c.strip() for c in node.acceptance_criteria if c.strip()]
    if criteria:
        section("Acceptance criteria", "\n".join(f"- [ ] {c}" for c in criteria))
    if node.level == "task" and node.kind:
        kind: str = node.kind
        if node.kind == "workload" and node.workload_profile:
            kind += f" (workload profile `{node.workload_profile}`)"
        section("Kind", kind)
    commands = [c for c in node.verify_commands if c.strip()]
    if commands:
        section("Verify commands", _fence(commands))
    if node.depends_on:
        known = dependencies or {}
        refs = []
        for dep in node.depends_on:
            if dep in known:
                repo, forge = known[dep]
                refs.append(f"- {issue_reference(node.repository, repo, forge.number)}")
            else:
                refs.append(f"- `{dep}`")
        section("Depends on", "\n".join(refs))
    section("Non-goals", node.non_goals)
    section("Constraints", node.constraints)
    parts.append(marker(node.plan_id, node.id))
    return "\n\n".join(parts) + "\n"
