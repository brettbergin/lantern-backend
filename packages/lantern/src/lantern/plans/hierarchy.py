"""What a plan looks like on a repository's forge, before anyone types.

A forge that links children natively (GitHub sub-issues) holds a plan as
``native``; one that does not (GitLab, by policy) holds it as level labels
and a managed ``checklist`` in the parent; a forge no backend answers
(Gitea) cannot hold one at all, and says why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from lantern.config import Config
from lantern.vcs.backends import capabilities_for
from lantern.vcs.protocol import Capability

Hierarchy = Literal["native", "checklist", "unsupported"]

#: How a forge is named in a reason a person reads.
FORGE_NAMES: dict[str, str] = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea"}


@dataclass(frozen=True, slots=True)
class RepositoryPlanning:
    hierarchy: Hierarchy
    reason: str | None = None

    @property
    def supported(self) -> bool:
        return self.hierarchy != "unsupported"


def repository_planning(kind: str) -> RepositoryPlanning:
    """How a repository on forge ``kind`` holds a plan."""
    report = capabilities_for(kind)
    name = FORGE_NAMES.get(kind, kind)
    if report is None:
        return RepositoryPlanning(
            "unsupported", f"this repository's forge can't hold plans: {name} is not supported"
        )
    state = report.get("sub_issues")
    if state is Capability.SUPPORTED:
        return RepositoryPlanning("native")
    if state is Capability.UNSUPPORTED:
        return RepositoryPlanning(
            "checklist",
            f"{name} has no native sub-issues here: children are listed in a managed "
            "checklist in the parent issue",
        )
    return RepositoryPlanning(
        "unsupported", f"could not tell whether {name} can link sub-issues; planning is off"
    )


def repository_planning_for(config: Config, repo: str) -> RepositoryPlanning:
    """How ``repo`` holds a plan on this server: its forge's answer, unless
    ``[planning] enabled`` (or its ``[vcs.repos.planning]`` override) is
    off, which is named as the reason."""
    if not config.planning_for(repo).enabled:
        return RepositoryPlanning(
            "unsupported",
            "planning is off for this repository ([planning] enabled = false)",
        )
    return repository_planning(str(config.vcs_kind_for(repo)))


def planning_available(config: Config) -> bool:
    """Whether this server offers planning: a configured repository that
    can hold a plan, or, with none configured, ``[vcs] kind`` with
    ``[planning]`` on."""
    repos = config.repo_list()
    if not repos:
        return config.planning.enabled and repository_planning(str(config.vcs.kind)).supported
    return any(repository_planning_for(config, entry.repo).supported for entry in repos)
