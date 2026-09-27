"""A plan node as the issue it becomes on the forge (#2341).

The body is the node's sections under markdown headings — what a person
reads on the forge and what a run's decompose reads back — followed by the
hidden marker ``<!-- sbx-plan: <plan_id>/<node_id> -->``. The marker is how
publishing stays idempotent: an issue an interrupted attempt already
created is found by it, never created twice.

Rewriting a published issue (:func:`rewrite_sections`, #2350) replaces
only the sections a person edited from the app and keeps everything else.
Reading a body back (:func:`parse_sections`, #2342) is limited to those
headings: a section runs from its heading to the next level-two heading, an
``sbx-plan`` comment or the end, fenced code is never read as a heading,
and everything outside our headings belongs to people and is not read.
Everything here is pure.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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


def section_blocks(
    node: PlanNode,
    *,
    dependencies: Mapping[str, tuple[str, ForgeRef]] | None = None,
) -> dict[str, str]:
    """Each non-empty section of ``node`` as it is rendered — its heading,
    a blank line, its text — by section, in the order they are rendered.

    ``dependencies`` maps each ``depends_on`` sibling id to its repository
    and forge reference, rendered as issue references; a dependency not in
    it (never the case once its sibling is published) is named by id.
    """
    blocks: dict[str, str] = {}

    def section(key: str, text: str) -> None:
        text = text.strip()
        if text:
            blocks[key] = f"## {HEADINGS[key]}\n\n{text}"

    section("goal", node.goal)
    section("context", node.context)
    criteria = [c.strip() for c in node.acceptance_criteria if c.strip()]
    if criteria:
        section("acceptance_criteria", "\n".join(f"- [ ] {c}" for c in criteria))
    if node.level == "task" and node.kind:
        kind: str = node.kind
        if node.kind == "workload" and node.workload_profile:
            kind += f" (workload profile `{node.workload_profile}`)"
        section("kind", kind)
    commands = [c for c in node.verify_commands if c.strip()]
    if commands:
        section("verify_commands", _fence(commands))
    if node.depends_on:
        known = dependencies or {}
        refs = []
        for dep in node.depends_on:
            if dep in known:
                repo, forge = known[dep]
                refs.append(f"- {issue_reference(node.repository, repo, forge.number)}")
            else:
                refs.append(f"- `{dep}`")
        section("depends_on", "\n".join(refs))
    section("non_goals", node.non_goals)
    section("constraints", node.constraints)
    return blocks


def render_body(
    node: PlanNode,
    *,
    dependencies: Mapping[str, tuple[str, ForgeRef]] | None = None,
) -> str:
    """The issue body for ``node``: its non-empty sections (see
    :func:`section_blocks`), then the marker."""
    parts = [*section_blocks(node, dependencies=dependencies).values()]
    parts.append(marker(node.plan_id, node.id))
    return "\n\n".join(parts) + "\n"


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


def _trim(lines: list[str]) -> str:
    """``lines`` without the blank lines around them."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


_KEPT = re.compile(f"{_CHILDREN_BLOCK.pattern}|{_COMMENT.pattern}", re.DOTALL)


def rewrite_sections(
    body: str, blocks: Mapping[str, str], keys: Iterable[str], *, whole: bool = False
) -> str:
    """``body`` with the sections ``keys`` names rewritten (#2350): each
    becomes its block in ``blocks``, or goes when it has none. A section
    named but absent is placed among ours in rendered order. Everything
    else keeps its text — a person's writing outside our headings, the
    sections not named (a ticked criterion stays ticked), the marker and
    the managed checklist; only the blank lines between parts are
    normalised. Sections are found the way :func:`section_texts` finds
    them; every copy of a named heading goes, so the one written is the
    one read back.

    ``whole`` is an issue adopted from the forge with none of our headings,
    whose whole text was read as its goal: ``blocks`` then replace all of
    it, and only the marker and the managed checklist stay."""
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    wanted = set(keys)
    rank = {key: index for index, key in enumerate(HEADINGS)}
    if whole:
        kept = [match.group(0) for match in _KEPT.finditer(text)]
        ordered = [blocks[key] for key in HEADINGS if key in blocks]
        return "\n\n".join([*ordered, *kept]) + "\n"
    # Each chunk: the section it holds (``None`` before any heading, ``""``
    # under a heading or comment that is not ours) and its lines.
    chunks: list[tuple[str | None, list[str]]] = [(None, [])]
    fence: str | None = None
    for line in text.split("\n"):
        if fence is not None:
            if _closes(line, fence):
                fence = None
        elif (opened := _FENCE_OPEN.match(line)) is not None:
            fence = opened.group("fence")
        else:
            heading = _HEADING.match(line)
            if heading is not None or line.lstrip().startswith("<!-- sbx-plan"):
                name = heading.group("name").casefold() if heading is not None else ""
                chunks.append((_BY_HEADING.get(name, ""), [line]))
                continue
        chunks[-1][1].append(line)
    present = {key for key, _ in chunks if key}
    absent = [key for key in HEADINGS if key in wanted and key not in present and key in blocks]
    ours = [index for index, (key, _) in enumerate(chunks) if key]
    # With none of our sections, new ones go before the marker (or last).
    anchor = next(
        (
            index
            for index, (key, lines) in enumerate(chunks)
            if key == "" and lines[0].lstrip().startswith("<!-- sbx-plan")
        ),
        len(chunks),
    )
    out: list[str] = []
    written: set[str] = set()
    for index, (key, lines) in enumerate(chunks):
        if not ours and index == anchor:
            out.extend(blocks[a] for a in absent)
            absent = []
        if not key:
            out.append(_trim(lines))
            continue
        before = [a for a in absent if rank[a] < rank[key]]
        out.extend(blocks[a] for a in before)
        absent = [a for a in absent if a not in before]
        if key not in wanted:
            out.append(_trim(lines))
        elif key not in written:
            written.add(key)
            out.append(blocks.get(key, ""))
        if index == ours[-1]:
            out.extend(blocks[a] for a in absent)
            absent = []
    out.extend(blocks[a] for a in absent)
    return "\n\n".join(part for part in out if part) + "\n"


def drop_reference(body: str, from_repo: str, repo: str, number: int) -> str:
    """``body`` with every item of its ``Depends on`` section that names
    issue ``number`` of ``repo`` removed (with the lines that continue it),
    and the section itself when nothing is left; everything else — the
    other items as written, included — is unchanged. A body whose section
    names no such issue comes back as it was."""
    texts = section_texts(body)
    if "depends_on" not in texts:
        return body
    target = (repo.casefold(), number)
    kept: list[str] = []
    dropping = False
    for line in texts["depends_on"].split("\n"):
        item = _ITEM.match(line)
        if item is not None:
            ref = item.group("text").strip().strip("`").strip()
            owner, _, digits = ref.rpartition("#")
            dropping = digits.isdigit() and ((owner or from_repo).casefold(), int(digits)) == target
        if not dropping:
            kept.append(line)
    if len(kept) == len(texts["depends_on"].split("\n")):
        return body
    remaining = _trim(kept)
    blocks = {"depends_on": f"## {HEADINGS['depends_on']}\n\n{remaining}"} if remaining else {}
    return rewrite_sections(body, blocks, ["depends_on"])


_ISSUE_URL = re.compile(
    r"^https?://[^/\s]+/(?P<repo>[^\s?#]+?)(?:/-)?/issues/(?P<number>\d+)/?(?:[?#]\S*)?$"
)


def parse_issue_url(url: str) -> tuple[str, int] | None:
    """``(repository, number)`` from an issue's web URL on either forge
    (``…/owner/name/issues/12``, ``…/group/project/-/issues/12``); ``None``
    for anything else."""
    match = _ISSUE_URL.match(url.strip())
    if match is None:
        return None
    return match.group("repo"), int(match.group("number"))


def repo_of(row: Mapping[str, Any]) -> str:
    """The ``owner/name`` (or ``group/project``) an issue payload's web URL
    says it lives in; empty when it has none."""
    url = str(row.get("html_url") or "")
    path = url.split("://", 1)[-1].split("/", 1)[-1]
    for sep in ("/-/issues/", "/issues/"):
        if sep in path:
            return path.split(sep, 1)[0]
    return ""
