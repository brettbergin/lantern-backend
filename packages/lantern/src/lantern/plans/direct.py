"""A person's direct writes to a published plan's issues (#2350).

After publish the forge wins, and lantern writes to it only when a person
asks. This module is the forge side of the three things a person can do
from the app; :class:`~lantern.plans.service.PlanService` holds the rules
and records the result:

- **Edit** a published node's title and sections: the issue is written at
  once through ``issue_update`` — the title when it changed, and in the body
  only the sections the edit changed (:func:`~lantern.plans.render.rewrite_sections`);
  a person's text outside our headings, the sections not edited, the marker
  and the managed checklist keep their text. The edit names the version of
  the issue it read (:func:`~lantern.plans.model.content_version`) and the
  issue is read first: one that changed since is refused, never written.
- **Attach** an existing open issue as a child one level down: a native
  sub-issue on GitHub, a line in the parent's managed checklist on GitLab
  (or where GitHub refuses a cross-repository link), then its level label.
- **Detach** a child: unlinked from its parent — the sub-issue link, any
  checklist line — and never closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any, Literal

from lantern.config import Config
from lantern.errors import LanternError
from lantern.plans.hierarchy import FORGE_NAMES, repository_planning
from lantern.plans.model import ForgeRef, Plan, PlanNode
from lantern.plans.reconcile import key_of
from lantern.plans.render import (
    HEADINGS,
    parse_sections,
    repo_of,
    rewrite_sections,
    section_blocks,
)
from lantern.vcs.checklist import (
    ChecklistEntry,
    ChecklistMangled,
    add_child,
    parse_checklist,
    remove_child,
    update_checklist,
)
from lantern.vcs.github.labels import LEVEL_DESCRIPTORS, LabelSpec, ensure_label
from lantern.vcs.protocol import IssueOps

Linked = Literal["native", "checklist"]

#: How long an error from the forge may run in a refusal.
ERROR_MAX = 300
#: What the forge answers for an issue that is not there: GitHub's 410 for
#: a deleted one, 404 on both forges for one it cannot find.
GONE = frozenset({404, 410})


def say(exc: BaseException) -> str:
    return (" ".join(str(exc).split()) or type(exc).__name__)[:ERROR_MAX]


def gone(exc: BaseException) -> bool:
    return getattr(exc, "http_status", None) in GONE


class LinkRefused(Exception):
    """The forge will not take the child under this parent; ``code`` names
    why for the client."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _hierarchy(config: Config, repo: str) -> str:
    """How ``repo``'s forge links children: by the backend's capability,
    as a reconcile reads them."""
    return repository_planning(str(config.vcs_kind_for(repo))).hierarchy


def refs(plan: Plan) -> dict[str, tuple[str, ForgeRef]]:
    """Every published node's repository and issue, by node id: what a
    ``Depends on`` section renders."""
    return {
        n.id: (n.repository, n.forge)
        for n in plan.nodes
        if n.state == "published" and n.forge is not None
    }


# -- edit ----------------------------------------------------------------------

#: The section each content field is rendered under.
_SECTION_OF = {key: key for key in HEADINGS} | {"workload_profile": "kind"}


def changed_sections(before: PlanNode, after: PlanNode) -> list[str]:
    """The sections whose rendering the edit from ``before`` to ``after``
    changes, in rendered order."""
    fields = [k for k in _SECTION_OF if getattr(before, k) != getattr(after, k)]
    wanted = {_SECTION_OF[k] for k in fields}
    return [key for key in HEADINGS if key in wanted]


def issue_write(
    plan: Plan, before: PlanNode, after: PlanNode, *, title: str, body: str
) -> tuple[str | None, str | None]:
    """What an edit from ``before`` (the node as the forge has it) to
    ``after`` writes to an issue now titled ``title`` with ``body``: the
    new title (``None``: unchanged) and the new body (``None``: unchanged,
    so a title edit never touches the body)."""
    new_title = after.title if after.title != " ".join(title.split()) else None
    keys = changed_sections(before, after)
    if not keys:
        return new_title, None
    whole = after.origin == "forge" and not parse_sections(body)
    blocks = section_blocks(after, dependencies=refs(plan))
    rewritten = rewrite_sections(body, blocks, keys, whole=whole)
    return new_title, None if rewritten == body else rewritten


# -- attach --------------------------------------------------------------------


def add_level_label(ops: IssueOps, config: Config, repo: str, number: int, level: str) -> None:
    """Put the level label on the issue, made sure of first; never the
    trigger or the workload label, so an attached task stays inert."""
    name = config.labels_for(repo).levels.get(level)
    if not name:
        raise LanternError(f"{repo} has no {level} label: planning is off there")
    spec = LabelSpec(name, *LEVEL_DESCRIPTORS[level], kind=level)
    if ensure_label(ops, repo, spec) == "failed":
        raise LanternError(f"could not make sure the label {name} exists on {repo}")
    ops.issue_labels_add(repo, number, [name])


def labelled(row: dict[str, Any], name: str) -> bool:
    for label in row.get("labels") or []:
        text = label.get("name") if isinstance(label, dict) else label
        if str(text or "").casefold() == name.casefold():
            return True
    return False


def link_child(
    ops: IssueOps, config: Config, parent: PlanNode, repo: str, number: int, title: str
) -> tuple[Linked, str | None]:
    """Link ``repo#number`` under ``parent``'s issue; how, and why a native
    link became a checklist line. A child GitHub says already has a parent
    (its 422) is refused: moving it is a person's decision on the forge."""
    assert parent.forge is not None  # nosec B101 - the service checks
    parent_repo, parent_number = parent.repository, parent.forge.number
    hierarchy = _hierarchy(config, parent_repo)
    reason: str | None = None
    if hierarchy == "native":
        try:
            ops.sub_issue_add(parent_repo, parent_number, child_repo=repo, child_number=number)
            return "native", None
        except LanternError as exc:
            if getattr(exc, "http_status", None) == 422:
                raise LinkRefused(
                    "already_has_parent",
                    f"{repo}#{number} is already a sub-issue of another issue; "
                    f"unlink it there first ({say(exc)})",
                ) from exc
            if parent_repo.casefold() == repo.casefold():
                raise
            forge = FORGE_NAMES.get(str(config.vcs_kind_for(parent_repo)), "the forge")
            reason = (
                f"{forge} refused the cross-repository sub-issue ({say(exc)}); "
                "it is listed in the parent's checklist instead"
            )
    elif hierarchy != "checklist":
        raise LinkRefused("planning_unsupported", "the parent's forge cannot hold a plan")
    entry = ChecklistEntry(ref=f"{repo}#{number}", title=title)
    update_checklist(ops, parent_repo, parent_number, lambda body: add_child(body, entry))
    return "checklist", reason


# -- detach --------------------------------------------------------------------


def _entry_key(ref: str) -> tuple[str, int] | None:
    repo, _, number = ref.rpartition("#")
    return key_of(repo, int(number)) if number.isdigit() else None


def unlink_child(
    ops: IssueOps, config: Config, parent: PlanNode, child: PlanNode
) -> Sequence[Linked]:
    """Unlink ``child``'s issue from ``parent``'s — its native sub-issue
    link and any line in the parent's managed checklist — and never close
    it; how it was linked (nothing: the forge no longer listed it). A
    broken checklist raises before anything is written, unless the child
    was linked natively."""
    assert parent.forge is not None and child.forge is not None  # nosec B101 - the service checks
    parent_repo, parent_number = parent.repository, parent.forge.number
    key = key_of(child.repository, child.forge.number)
    unlinked: list[Linked] = []
    if _hierarchy(config, parent_repo) == "native":
        rows = ops.sub_issues_list(parent_repo, parent_number)
        if any(_listed(row, parent_repo, key) for row in rows):
            ops.sub_issue_remove(
                parent_repo,
                parent_number,
                child_repo=child.repository,
                child_number=child.forge.number,
            )
            unlinked.append("native")
    body = str(ops.issue_get(parent_repo, parent_number).get("body") or "")
    try:
        entries = parse_checklist(body)
    except ChecklistMangled:
        if unlinked:
            return unlinked
        raise
    for entry in entries:
        if _entry_key(entry.ref) == key:
            ops.issue_update(parent_repo, parent_number, body=remove_child(body, entry.ref))
            unlinked.append("checklist")
            break
    return unlinked


def _listed(row: Any, parent_repo: str, key: tuple[str, int]) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("number"), int):
        return False
    return key_of(repo_of(row) or parent_repo, int(row["number"])) == key


def without_dependency(siblings: Iterable[PlanNode], node_id: str) -> list[PlanNode]:
    """The siblings that depended on ``node_id``, depending on it no more."""
    return [
        replace(s, depends_on=tuple(d for d in s.depends_on if d != node_id))
        for s in siblings
        if node_id in s.depends_on
    ]
