"""A plan node as the issue it becomes on the forge (#2341).

The body is the node's sections under markdown headings — what a person
reads on the forge and what a run's decompose reads back — followed by the
hidden marker ``<!-- sbx-plan: <plan_id>/<node_id> -->``. The marker is how
publishing stays idempotent: an issue an interrupted attempt already
created is found by it, never created twice.

Reading a body back (:func:`parse_sections`, #2342) is limited to those
headings: a section runs from its heading to the next level-two heading, an
``sbx-plan`` comment or the end, fenced code is never read as a heading,
and everything outside our headings belongs to people and is not read.
Everything here is pure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

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


#: Our headings, by the section each holds, in the order they are rendered.
HEADINGS: dict[str, str] = {
    "goal": "Goal",
    "context": "Context",
    "acceptance_criteria": "Acceptance criteria",
    "kind": "Kind",
    "verify_commands": "Verify commands",
    "depends_on": "Depends on",
    "non_goals": "Non-goals",
    "constraints": "Constraints",
}
_BY_HEADING = {heading.casefold(): key for key, heading in HEADINGS.items()}
_HEADING = re.compile(r"^##[ \t]+(?P<name>.*?)[ \t#]*$")
_FENCE_OPEN = re.compile(r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})")
_ITEM = re.compile(r"^\s*[-*+][ \t]+(?:\[[ xX]\][ \t]*)?(?P<text>.*)$")
_KIND = re.compile(
    r"^(?P<kind>code|workload)(?:\s*\(workload profile `(?P<profile>[^`]+)`\))?$", re.IGNORECASE
)
_COMMENT = re.compile(r"<!--\s*/?sbx-plan[^>]*-->")
_CHILDREN_BLOCK = re.compile(
    r"<!--\s*sbx-plan:children\s*-->.*?<!--\s*/sbx-plan:children\s*-->", re.DOTALL
)
#: Sections that hold a list, one entry per list item.
LIST_SECTIONS = frozenset({"acceptance_criteria", "verify_commands", "depends_on"})


def _closes(line: str, fence: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= len(fence) and set(stripped) == {fence[0]}


def section_texts(body: str) -> dict[str, str]:
    """The raw text under each of our headings in ``body``, by section; the
    first heading of a name wins. A section ends at the next level-two
    heading, at an ``sbx-plan`` comment (the marker, the managed children
    block) or at the end; a heading inside fenced code is text."""
    out: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    fence: str | None = None

    def close() -> None:
        if current is not None and current not in out:
            out[current] = "\n".join(lines).strip()

    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if fence is not None:
            if _closes(line, fence):
                fence = None
        elif (opened := _FENCE_OPEN.match(line)) is not None:
            fence = opened.group("fence")
        else:
            heading = _HEADING.match(line)
            if heading is not None or line.lstrip().startswith("<!-- sbx-plan"):
                close()
                name = heading.group("name").casefold() if heading is not None else ""
                current = _BY_HEADING.get(name)
                lines = []
                continue
        if current is not None:
            lines.append(line)
    close()
    return out


def _items(text: str) -> tuple[str, ...]:
    """A markdown list's entries; a line that is not an item continues the
    one before it."""
    items: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _ITEM.match(line)
        if match is not None:
            items.append(match.group("text").strip())
        elif items:
            items[-1] = f"{items[-1]} {line.strip()}".strip()
        else:
            items.append(line.strip())
    return tuple(item for item in items if item)


def _commands(text: str) -> tuple[str, ...]:
    """The lines of the section's fenced block, or its lines when it has
    none."""
    lines = text.splitlines()
    opened = _FENCE_OPEN.match(lines[0]) if lines else None
    if opened is not None:
        fence = opened.group("fence")
        inner: list[str] = []
        for line in lines[1:]:
            if _closes(line, fence):
                break
            inner.append(line)
        lines = inner
    return tuple(line for line in lines if line.strip())


def parse_sections(body: str) -> dict[str, Any]:
    """Our sections as ``body`` has them: text sections as strings, list
    sections as tuples (``depends_on`` as the issue references written,
    unresolved), ``kind`` as its raw text. A section whose heading is
    absent is absent here."""
    out: dict[str, Any] = {}
    for key, text in section_texts(body).items():
        if key == "verify_commands":
            out[key] = _commands(text)
        elif key in LIST_SECTIONS:
            out[key] = _items(text)
        else:
            out[key] = text
    return out


def parse_kind(text: str) -> tuple[str | None, str | None] | None:
    """``(kind, workload_profile)`` from a rendered Kind section; ``(None,
    None)`` for an empty one, ``None`` for text that names neither kind."""
    text = " ".join(text.split())
    if not text:
        return None, None
    match = _KIND.match(text)
    if match is None:
        return None
    return match.group("kind").lower(), match.group("profile")


def free_text(body: str) -> str:
    """``body`` without the ``sbx-plan`` marker and managed children block:
    what a person wrote on an issue sbxloop did not render."""
    return _COMMENT.sub("", _CHILDREN_BLOCK.sub("", body.replace("\r\n", "\n"))).strip()


def repo_of(row: Mapping[str, Any]) -> str:
    """The ``owner/name`` (or ``group/project``) an issue payload's web URL
    says it lives in; empty when it has none."""
    url = str(row.get("html_url") or "")
    path = url.split("://", 1)[-1].split("/", 1)[-1]
    for sep in ("/-/issues/", "/issues/"):
        if sep in path:
            return path.split(sep, 1)[0]
    return ""
