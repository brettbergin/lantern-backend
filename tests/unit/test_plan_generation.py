"""A breakdown end to end: a plan item admitted for a node, dispatched by the
daemon to the REAL engine (echo backend, fake sbx), the planner's scripted
answer delivered to the plan record as ``proposed`` children — and nothing
written to the forge. The engine's own rules are in ``test_engine_plan.py``
and the route's in ``tests/api/test_plans.py``; this proves the wiring
between them: the item names its node, the daemon hands the run a desk over
the plan service, and the service writes the level under the node's rules
with the ``plan.generation.*`` events scoped to the run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from sbxloop import hostgit
from sbxloop.config import Config
from sbxloop.daemon.controls.intake import PlanAdmission, plan_item
from sbxloop.daemon.controls.results import ControlError
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.sources import ApiSource, CompositeSource
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ApiEventRow
from sbxloop.engine.store import StateStore
from sbxloop.ghids import api_item_id
from sbxloop.plans.service import PlanService
from sbxloop.plans.store import PlanStore
from sbxloop.sbx.cli import SbxCLI
from tests.conftest import FakeSbx
from tests.fakes.gitrepo import make_repo
from tests.unit.test_daemon_loop import FakeSource
from tests.unit.test_engine import Harness
from tests.unit.test_engine_plan import answer, code_task, workload_task

REPO = "o/app"
PERSON = {"kind": "client", "id": "c1", "display": "Pat", "via": "api"}


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    origin = make_repo(tmp_path, "upstream", {"README.md": "# app\n"})
    monkeypatch.setattr(
        hostgit,
        "clone_from_remote",
        lambda url, target, branch, **kw: hostgit.clone_for_run(origin, target, branch),
    )
    return Harness(fake_sbx, tmp_path, monkeypatch)


class World:
    """The daemon, its stores and a plan with one epic to break down."""

    def __init__(self, harness: Harness, **config: Any) -> None:
        self.config = Config.model_validate(
            {
                "home": str(harness.home.root),
                "limits": {"disk_warn": 0, "disk_abort": 0, "mem_warn": 0},
                "github": {"repos": [{"repo": REPO}]},
                "workloads": [{"name": "research", "description": "reads the web"}],
            }
            | config
        )
        self.store = StateStore(harness.home.state_db)
        self.dstore = DaemonStore(harness.home.state_db)
        self.plans = PlanService(PlanStore(self.dstore), lambda: self.config)
        # The forge's source sees every GitHub call the daemon makes; the
        # plan item is an API item, so it is routed to the API source.
        self.github = FakeSource()
        self.loop = DaemonLoop(
            self.config,
            store=self.store,
            dstore=self.dstore,
            source=CompositeSource(self.github, None, None, ApiSource()),
            sbx=SbxCLI(binary=str(harness.fake_sbx.binary)),
            worker_python=sys.executable,
            install_workers=False,
        )
        plan = self.plans.create(
            level="epic",
            repository=REPO,
            sections={"title": "Export reports", "goal": "download reports as CSV"},
            now=1.0,
            actor=PERSON,
        )
        self.plan_id, self.epic_id = plan.id, plan.root_id
        # One task a person drafted (it stays) and one the planner proposed
        # last time (the new proposal replaces it).
        plan, self.kept_id = self.plans.add_node(
            plan.id,
            expected_revision=plan.revision,
            parent_id=self.epic_id,
            repository=None,
            sections={"title": "Person's task", "kind": "code"},
            position=None,
            now=2.0,
            actor=PERSON,
        )
        plan, stale_id = self.plans.add_node(
            plan.id,
            expected_revision=plan.revision,
            parent_id=self.epic_id,
            repository=None,
            sections={"title": "Old proposal", "kind": "code"},
            position=None,
            now=3.0,
            actor=PERSON,
        )
        old = plan.node(stale_id)
        assert old is not None
        from dataclasses import replace

        plan = self.plans.store.apply(
            plan.id,
            expected_revision=plan.revision,
            now=4.0,
            upsert=[replace(old, state="proposed", origin="planner")],
        )
        self.revision = plan.revision

    def admit(self, note: str = "") -> str:
        item = plan_item(
            self.loop,
            PlanAdmission(self.plan_id, self.epic_id, expected_revision=self.revision, note=note),
            item_id=api_item_id("plan:k1"),
        )
        self.dstore.upsert_new(item, 5.0)
        return item.item_id

    def events(self, type_: str) -> list[ApiEventRow]:
        with self.dstore.read() as session:
            return list(
                session.scalars(
                    select(ApiEventRow).where(ApiEventRow.type == type_).order_by(ApiEventRow.seq)
                )
            )


def test_a_breakdown_runs_in_the_sandbox_and_lands_in_the_plan(harness: Harness) -> None:
    world = World(harness)
    item_id = world.admit(note="CSV only")
    harness.script([answer(code_task("c1"), code_task("c2", deps=["c1"]), workload_task("c3"))])

    result = world.loop.tick()

    assert result.dispatched == item_id
    assert result.outcome == "done", result
    item = world.dstore.get(item_id)
    assert item is not None and item.state == "done" and item.run_id is not None
    run = world.store.get_run(item.run_id)
    assert run.kind == "plan" and run.state == "completed"
    assert run.outcome.startswith("Propose the tasks of “Export reports”\n\nCSV only")
    assert [p.sink for p in run.published] == ["plan"]
    # The level under the epic: the person's task stays, the old proposal
    # is gone, the planner's three are proposed with their links mapped.
    plan = world.plans.get(world.plan_id)
    assert plan.revision == world.revision + 1, "one write, one revision"
    children = plan.children(world.epic_id)
    assert [c.title for c in children] == ["Person's task", "Task c1", "Task c2", "Survey c3"]
    kept, c1, c2, c3 = children
    assert (kept.id, kept.state, kept.origin) == (world.kept_id, "draft", "person")
    for child in (c1, c2, c3):
        assert child.state == "proposed" and child.origin == "planner"
        assert child.repository == REPO and child.level == "task"
    assert c1.kind == "code" and c1.verify_commands == ("make test",)
    assert c1.acceptance_criteria == ("c1 works",)
    assert c2.depends_on == (c1.id,)
    assert c3.kind == "workload" and c3.workload_profile == "research"
    assert [c.position for c in children] == [0, 1, 2, 3]
    # The generation's events, scoped to the run and its item.
    (started,) = world.events("plan.generation.started")
    (proposed,) = world.events("plan.generation.proposed")
    assert world.events("plan.generation.failed") == []
    public = f"run_{item.run_id}"
    assert json.loads(started.data_json or "{}") == {
        "plan_id": world.plan_id,
        "node_id": world.epic_id,
        "run_id": public,
    }
    assert json.loads(proposed.data_json or "{}") == {
        "plan_id": world.plan_id,
        "node_id": world.epic_id,
        "run_id": public,
        "count": 3,
    }
    assert proposed.run_id == item.run_id and proposed.item_id == item_id
    # Nothing reached the forge: its source was never asked a thing.
    assert world.github.calls == []
    assert harness.sandboxes_left() == []


def test_a_proposal_invalid_twice_is_a_failed_generation(harness: Harness) -> None:
    world = World(harness, daemon={"max_attempts_per_item": 1})
    item_id = world.admit()
    bad = answer(code_task("c1", acceptance_criteria=[]))
    harness.script([bad, bad])

    result = world.loop.tick()

    assert result.outcome == "failed", result
    item = world.dstore.get(item_id)
    assert item is not None and item.run_id is not None
    (failed,) = world.events("plan.generation.failed")
    data = json.loads(failed.data_json or "{}")
    assert data["run_id"] == f"run_{item.run_id}"
    assert data["reason"].startswith("the planner's proposal was invalid twice:")
    assert world.events("plan.generation.proposed") == []
    plan = world.plans.get(world.plan_id)
    assert plan.revision == world.revision, "a failed generation writes nothing"
    assert "Old proposal" in [c.title for c in plan.children(world.epic_id)]


def test_a_node_already_being_broken_down_is_not_queued_twice(harness: Harness) -> None:
    world = World(harness)
    world.admit()
    with pytest.raises(ControlError) as refused:
        plan_item(
            world.loop,
            PlanAdmission(world.plan_id, world.epic_id, expected_revision=world.revision),
            item_id=api_item_id("plan:k2"),
        )
    assert refused.value.code == "already_in_progress"
    assert refused.value.detail["plan_code"] == "generation_in_progress"
