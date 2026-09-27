"""The managed children checklist in a parent issue's body (#2339).

A forge with no native sub-issues (GitLab answers ``sub_issues``
``UNSUPPORTED``, by policy) still shows a published plan's hierarchy: the
parent's description carries one managed block, between
``<!-- sbx-plan:children -->`` and ``<!-- /sbx-plan:children -->``, with a
``- [ ] group/project#N title`` line per child and ``- [x]`` once the child
is closed. sbxloop rewrites that block and nothing else: the rest of the
description belongs to people.

A block a person broke — a marker missing, a second block, a line that is
not a child — is reported as :class:`ChecklistMangled` naming what is
wrong, and left as it is. Repairing it silently could throw away a
person's edit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from sbxloop.errors import SbxloopError
from sbxloop.vcs.protocol import IssueOps

START = "<!-- sbx-plan:children -->"
END = "<!-- /sbx-plan:children -->"

# ``- [ ] group/sub/project#12 A title``; ``[x]`` or ``[X]`` when closed.
_ENTRY = re.compile(r"^- \[(?P<mark>[ xX])\] (?P<ref>[\w.-]+(?:/[\w.-]+)+#\d+)(?: (?P<title>.*))?$")


class ChecklistMangled(SbxloopError):
    """The parent's managed block is not one sbxloop can rewrite safely."""


@dataclass(frozen=True, slots=True)
class ChecklistEntry:
    """One child: its cross-project reference (``group/project#N``), its
    title as last written, and whether it is closed."""

    ref: str
    title: str
    closed: bool = False

    def line(self) -> str:
        title = " ".join(self.title.split())
        return f"- [{'x' if self.closed else ' '}] {self.ref}" + (f" {title}" if title else "")


def render_checklist(entries: list[ChecklistEntry]) -> str:
    """The managed block for ``entries``, markers included."""
    return "\n".join([START, *(entry.line() for entry in entries), END])


def _locate(body: str) -> tuple[int, int] | None:
    """Where the one managed block sits (its start and the end of its
    closing marker), ``None`` when there is none; a broken block raises."""
    starts = [m.start() for m in re.finditer(re.escape(START), body)]
    ends = [m.start() for m in re.finditer(re.escape(END), body)]
    if not starts and not ends:
        return None
    if len(starts) > 1 or len(ends) > 1:
        raise ChecklistMangled("the parent's description has more than one sbx-plan:children block")
    if not ends:
        raise ChecklistMangled("the sbx-plan:children block has no closing marker")
    if not starts:
        raise ChecklistMangled("the sbx-plan:children block has no opening marker")
    if ends[0] < starts[0]:
        raise ChecklistMangled("the sbx-plan:children markers are out of order")
    return starts[0], ends[0] + len(END)


def parse_checklist(body: str) -> list[ChecklistEntry]:
    """The children the managed block lists, in order; none when there is
    no block."""
    span = _locate(body)
    if span is None:
        return []
    inner = body[span[0] + len(START) : span[1] - len(END)]
    entries: list[ChecklistEntry] = []
    for number, line in enumerate(inner.strip("\n").splitlines(), start=1):
        if not line.strip():
            continue
        match = _ENTRY.match(line.strip())
        if match is None:
            raise ChecklistMangled(
                f"line {number} of the sbx-plan:children block is not a child: {line.strip()!r}"
            )
        entries.append(
            ChecklistEntry(
                ref=match.group("ref"),
                title=match.group("title") or "",
                closed=match.group("mark") in "xX",
            )
        )
    return entries


def write_checklist(body: str, entries: list[ChecklistEntry]) -> str:
    """``body`` with its managed block replaced by ``entries`` — or, when it
    has none, a block appended after a blank line. Nothing outside the
    block changes."""
    span = _locate(body)
    block = render_checklist(entries)
    if span is None:
        base = body.rstrip("\n")
        return f"{base}\n\n{block}\n" if base else f"{block}\n"
    return body[: span[0]] + block + body[span[1] :]


def _edit(body: str, change: Callable[[list[ChecklistEntry]], list[ChecklistEntry]]) -> str:
    entries = parse_checklist(body)
    changed = change(entries)
    return body if changed == entries else write_checklist(body, changed)


def add_child(body: str, entry: ChecklistEntry) -> str:
    """``entry`` listed last; a child already listed (by reference) stays
    as it is."""
    return _edit(
        body,
        lambda entries: entries if any(e.ref == entry.ref for e in entries) else [*entries, entry],
    )


def remove_child(body: str, ref: str) -> str:
    """The child ``ref`` unlisted; an absent one changes nothing."""
    return _edit(body, lambda entries: [e for e in entries if e.ref != ref])


def set_child_closed(body: str, ref: str, *, closed: bool) -> str:
    """The child ``ref`` ticked (or unticked)."""
    return _edit(
        body,
        lambda entries: [replace(e, closed=closed) if e.ref == ref else e for e in entries],
    )


def update_checklist(ops: IssueOps, repo: str, number: int, change: Callable[[str], str]) -> bool:
    """Apply ``change`` to the parent issue's description and write it back
    only when it changed; whether it was written. A mangled block raises
    before anything is written."""
    body = str(ops.issue_get(repo, number).get("body") or "")
    changed = change(body)
    if changed == body:
        return False
    ops.issue_update(repo, number, body=changed)
    return True
