"""A plan that advances itself, end to end with no person in any step.

An owner writes four grants and flips a plan to ``auto``. From then on the
daemon's own ticks do everything: the plan driver admits the breakdown as
the planner; the daemon dispatches it to the REAL engine (echo backend,
fake sbx), where the planner's scripted proposal and the critic's scripted
verdict are delivered to the plan; the driver approves the level as the
critic, waits out ``[delegation] publish_delay_s``, publishes it to the fake
forge, and starts the epic run, which admits the first task. The decisions
ledger, the operations log and the plan record say who did each step.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from lantern import hostgit
from lantern.config import Config
from lantern.daemon.controls.delegation import parse_conditions
from lantern.daemon.loop import DaemonLoop
from lantern.daemon.sources import ApiSource, CompositeSource, GitHubIssueSource
from lantern.daemon.store import DaemonStore
from lantern.engine.store import StateStore
from lantern.sbx.cli import SbxCLI
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.fakes.gitrepo import make_repo
from tests.unit.test_daemon_loop import Clock
from tests.unit.test_daemon_sources import LABELS
from tests.unit.test_engine import Harness
from tests.unit.test_engine_plan import code_task
from tests.unit.test_plan_driver import Box

REPO = "o/r"
OWNER = {"kind": "client", "id": "usr_owner", "display": "Owner", "via": "api"}
DELAY = 900


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    origin = make_repo(tmp_path, "upstream", {"README.md": "# app\n"})
    monkeypatch.setattr(
        hostgit,
        "clone_from_remote",
        lambda url, target, branch, **kw: hostgit.clone_for_run(origin, target, branch),
    )
    return Harness(fake_sbx, tmp_path, monkeypatch)


def _proposal() -> dict[str, Any]:
    return {
        "json": {
            "root": {
                "title": "Export reports",
                "goal": "Download reports as CSV",
                "context": "The reports module handles exports",
                "acceptance_criteria": ["Reports export as CSV"],
            },
            "children": [code_task("c1"), code_task("c2", deps=["c1"])],
        }
    }


APPROVE = {"json": {"verdict": "approve", "reasons": []}}


class _Provisioner:
    def clone_token(self, repo: str) -> None:
        return None


class Forge(Box):
    """The fake forge as the daemon holds it, down to the clone token a
    workspace refresh asks for (none: the clone is the test's origin)."""

    provisioner = _Provisioner()


def test_an_owner_states_the_grants_once_and_the_plan_runs_itself(harness: Harness) -> None:
    config = Config.model_validate(
        {
            "home": str(harness.home.root),
            "limits": {"disk_warn": 0, "disk_abort": 0, "mem_warn": 0},
            "github": {"repos": [{"repo": REPO}]},
            "delegation": {"publish_delay_s": DELAY},
        }
    )
    store = StateStore(harness.home.state_db)
    dstore = DaemonStore(harness.home.state_db)
    fake = FakeGithub()
    clock = Clock(t=2_000_000_000.0)
    loop = DaemonLoop(
        config,
        store=store,
        dstore=dstore,
        source=CompositeSource(
            GitHubIssueSource(lambda: fake, REPO, LABELS, host="db"),  # type: ignore[arg-type]
            None,
            None,
            ApiSource(),
        ),
        sbx=SbxCLI(binary=str(harness.fake_sbx.binary)),
        worker_python=sys.executable,
        install_workers=False,
        clock=clock,
    )
    loop.github = Forge(fake)

    # The owner, once: four grants, and the plan set to advance itself.
    for agent, action, conditions in (
        ("planner", "plan.breakdown", {"repositories": [REPO]}),
        ("critic", "plan.approve", {"repositories": [REPO], "require_review": True}),
        ("critic", "plan.publish", {"repositories": [REPO], "require_review": True}),
        ("critic", "plan.run", {"repositories": [REPO]}),
    ):
        loop.delegation.create_grant(
            agent_slug=agent,
            action=action,
            conditions=parse_conditions(action, conditions),
            daily_limit=None,
            enabled=True,
            note=None,
            created_by=OWNER["id"],
            created_by_display=OWNER["display"],
            now=clock(),
        )
    plan = loop.plans.create(
        level="epic",
        repository=REPO,
        sections={"title": "Export reports", "goal": "download reports as CSV"},
        now=clock(),
        actor=OWNER,
        advance="auto",
    )
    revisions = {"created": plan.revision}

    # Tick 1: the driver queues the breakdown as the planner, and the same
    # tick dispatches it: the planner proposes, the critic approves.
    harness.script([_proposal(), APPROVE])
    clock.t += 1
    first = loop.tick()
    assert first.outcome == "done", first
    assert harness.consumed() == 2
    plan = loop.plans.get(plan.id)
    assert plan.root.review is not None and plan.root.review.verdict == "approve"
    assert [c.state for c in plan.children(plan.root_id)] == ["proposed", "proposed"]

    # Tick 2: approved as the critic. Ticks inside the window publish nothing.
    clock.t += 1
    loop.tick()
    approved_at = clock()
    plan = loop.plans.get(plan.id)
    assert [c.approved_by for c in plan.children(plan.root_id)] == ["agent:critic"] * 2
    clock.t = approved_at + DELAY - 1
    loop.tick()
    assert loop.plans.get(plan.id).root.state != "published"

    # Past the window: published to the forge as the critic.
    clock.t = approved_at + DELAY
    loop.tick()
    plan = loop.plans.get(plan.id)
    assert plan.root.state == "published" and plan.root.published_by == "agent:critic"
    assert [c.published_by for c in plan.children(plan.root_id)] == ["agent:critic"] * 2
    assert [title for title, _, _ in fake.issues_created] == [
        "Export reports",
        "Task c1",
        "Task c2",
    ]

    # The epic run, started as the critic. (Its first task is queued; it is
    # not dispatched here: that is an ordinary code run.)
    clock.t += 1
    loop.plan_driver.tick(clock())
    run = loop.epic_runs.runs.latest(plan.id, plan.root_id)
    assert run is not None and run.started_by == "agent:critic"
    assert [t.state for t in run.tasks] == ["queued", "waiting"]

    # Zero human calls: every step on the ledger is an agent's, allowed.
    rows = sorted(loop.delegation.page(limit=50), key=lambda r: r.at)
    assert [(r.agent_slug, r.action, r.outcome) for r in rows] == [
        ("planner", "plan.breakdown", "allow"),
        ("critic", "plan.approve", "allow"),
        ("critic", "plan.publish", "allow"),
        ("critic", "plan.run", "allow"),
    ]
    assert loop.delegation.unresolved_count() == 0
    ops = {o.id: o for o in loop.operations.recent(limit=50)}
    assert [ops[r.operation_id].actor["id"] for r in rows] == [  # type: ignore[index]
        "agent:planner",
        "agent:critic",
        "agent:critic",
        "agent:critic",
    ]
    assert all(ops[r.operation_id].state == "succeeded" for r in rows)  # type: ignore[index]
    people = [o for o in ops.values() if o.actor.get("kind") != "agent"]
    assert people == [], "no person's operation anywhere after the owner's setup"
    assert revisions["created"] < plan.revision
