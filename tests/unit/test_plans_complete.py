"""Closing finished epics and initiatives with a summary (#2349).

The forge is the record: an epic is finished when every one of its
published tasks' issues is closed there, and an initiative when every one
of its epics' is. A finished one gets a summary comment (once — the comment
carries a hidden marker) and is closed as completed, behind ``[planning]
close_completed``; a task an epic run skipped but nobody closed keeps its
epic open. A GitLab parent's managed checklist has each closed child ticked.
The plans are published with the real walk against the forge fakes first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from lantern.config import Config
from lantern.daemon.store import DaemonStore
from lantern.db.api_models import ApiEventRow
from lantern.plans.complete import Completion, summary_marker
from lantern.plans.epicrun import EpicRun, EpicRunTask
from lantern.plans.model import Plan, PlanNode
from lantern.plans.publish import publish_level
from lantern.plans.store import PlanStore
from lantern.vcs.checklist import parse_checklist
from tests.fakes.fake_github import FakeGithub
from tests.fakes.fake_gitlab import FakeGitlab

NOW = 1_800_000_000.0


def _config(tmp_path: Path, *, kind: str = "github", **planning: Any) -> Config:
    repo = "acme/widgets" if kind == "gitlab" else "o/r"
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "vcs": {"kind": kind, "repos": [{"repo": repo}]},
            **({"planning": planning} if planning else {}),
        }
    )


def _node(node_id: str, **fields: Any) -> PlanNode:
    base: dict[str, Any] = {
        "id": node_id,
        "plan_id": "plan_1",
        "parent_id": None,
        "position": 0,
        "level": "task",
        "repository": "o/r",
        "state": "approved",
        "origin": "person",
        "title": node_id.upper(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(fields)
    return PlanNode(**base)


def _initiative(repo: str) -> list[PlanNode]:
    """An initiative of two epics: E1 with tasks A and B, E2 with C."""
    return [
        _node("ini", level="initiative", state="draft", repository=repo),
        _node("e1", level="epic", parent_id="ini", position=0, repository=repo),
        _node("e2", level="epic", parent_id="ini", position=1, repository=repo),
        _node("a", parent_id="e1", position=0, repository=repo, kind="code"),
        _node("b", parent_id="e1", position=1, repository=repo, kind="code"),
        _node("c", parent_id="e2", position=0, repository=repo, kind="code"),
    ]


def _lone_epic(repo: str) -> list[PlanNode]:
    return [
        _node("e1", level="epic", state="draft", repository=repo),
        _node("a", parent_id="e1", position=0, repository=repo, kind="code"),
        _node("b", parent_id="e1", position=1, repository=repo, kind="code"),
    ]


def _published(
    tmp_path: Path, ops: Any, config: Config, nodes: list[PlanNode]
) -> tuple[PlanStore, dict[str, int]]:
    """The plan saved and published level by level; each node's number."""
    store = PlanStore(DaemonStore(tmp_path / "state.db"))
    root = nodes[0]
    store.create(
        Plan(
            id="plan_1",
            workspace_id="default",
            root_id=root.id,
            archived=False,
            created_by=None,
            created_by_display=None,
            created_at=NOW,
            updated_at=NOW,
            revision=1,
            nodes=tuple(nodes),
        ),
        events=[],
        actor=None,
    )
    for parent in [n for n in nodes if n.level != "task"]:
        plan = store.get("plan_1")
        assert plan is not None
        result = publish_level(
            ops,
            store=store,
            config=config,
            plan=plan,
            node_id=parent.id,
            clock=lambda: NOW,
            actor=None,
        )
        assert result.failed == [], result.results
    plan = store.get("plan_1")
    assert plan is not None
    numbers = {n.id: n.forge.number for n in plan.nodes if n.forge is not None}
    assert set(numbers) == {n.id for n in nodes}
    return store, numbers


def _completion(ops: Any, store: PlanStore, config: Config) -> Completion:
    return Completion(ops, store=store, config=config, clock=lambda: NOW + 60)


def _close_on_github(fake: FakeGithub, number: int) -> None:
    for issue in fake.existing_issues:
        if issue["number"] == number:
            issue["state"] = "closed"


def _forge_state(store: PlanStore, node_id: str) -> str | None:
    plan = store.get("plan_1")
    node = None if plan is None else plan.node(node_id)
    return None if node is None or node.forge is None else node.forge.state


def _changes(store: PlanStore) -> list[tuple[str, str]]:
    with store.dstore.read() as session:
        rows = session.scalars(
            select(ApiEventRow)
            .where(ApiEventRow.type == "plan.node.changed")
            .order_by(ApiEventRow.seq)
        )
        data = [json.loads(r.data_json) for r in rows]
    return [(d["node_id"], d["change"]) for d in data if d["change"] != "published"]


def _summaries(fake: FakeGithub, number: int) -> list[str]:
    return [body for n, body in fake.issue_answers if n == number]


def _run(**states: str) -> EpicRun:
    return EpicRun(
        id="erun_1",
        plan_id="plan_1",
        node_id="e1",
        state="completed",
        started_by="clt_ada",
        started_by_display="Ada",
        created_at=NOW,
        updated_at=NOW,
        tasks=tuple(
            EpicRunTask(node_id=k, position=i, state=v)  # type: ignore[arg-type]
            for i, (k, v) in enumerate(states.items())
        ),
    )


class TestEpic:
    def test_every_task_closed_summarises_and_closes_the_epic_once(self, tmp_path: Path) -> None:
        config, fake = _config(tmp_path), FakeGithub()
        store, n = _published(tmp_path, fake, config, _lone_epic("o/r"))
        _close_on_github(fake, n["a"])
        _close_on_github(fake, n["b"])

        result = _completion(fake, store, config).epic(
            "plan_1",
            "e1",
            epic_run=_run(a="landed", b="closed"),
            link=lambda task, ran: "https://github.com/o/r/pull/77" if task.id == "a" else None,
        )

        assert result.closed == ["e1"] and result.open == []
        (summary,) = _summaries(fake, n["e1"])
        assert summary.startswith(summary_marker("plan_1", "e1"))
        assert "Every task of this epic is closed" in summary
        assert "(2 tasks: 1 landed, 1 closed)" in summary
        assert f"- #{n['a']} A — landed: https://github.com/o/r/pull/77" in summary
        assert f"- #{n['b']} B — closed" in summary
        assert "Epic run `erun_1`, started by Ada." in summary
        assert fake.issues_closed == [(n["e1"], "completed")]
        assert [_forge_state(store, k) for k in ("e1", "a", "b")] == ["closed"] * 3
        assert _changes(store) == [("a", "closed"), ("b", "closed"), ("e1", "completed")]

        again = _completion(fake, store, config).epic("plan_1", "e1")
        assert again.closed == [] and again.recorded == []
        assert len(_summaries(fake, n["e1"])) == 1
        assert fake.issues_closed == [(n["e1"], "completed")]

    def test_an_open_task_keeps_the_epic_open(self, tmp_path: Path) -> None:
        config, fake = _config(tmp_path), FakeGithub()
        store, n = _published(tmp_path, fake, config, _lone_epic("o/r"))
        _close_on_github(fake, n["a"])

        result = _completion(fake, store, config).epic("plan_1", "e1")

        assert result.open == ["b"] and result.closed == []
        assert fake.issue_answers == [] and fake.issues_closed == []
        # The closed task is recorded all the same.
        assert (_forge_state(store, "a"), _forge_state(store, "e1")) == ("closed", "open")

    def test_a_skipped_task_left_open_keeps_the_epic_open_until_it_is_closed(
        self, tmp_path: Path
    ) -> None:
        config, fake = _config(tmp_path), FakeGithub()
        store, n = _published(tmp_path, fake, config, _lone_epic("o/r"))
        run = _run(a="landed", b="skipped")
        _close_on_github(fake, n["a"])

        held = _completion(fake, store, config).epic("plan_1", "e1", epic_run=run)
        assert held.open == ["b"] and fake.issues_closed == []

        _close_on_github(fake, n["b"])
        done = _completion(fake, store, config).epic("plan_1", "e1", epic_run=run)
        assert done.closed == ["e1"]
        (summary,) = _summaries(fake, n["e1"])
        assert "(2 tasks: 1 landed, 1 skipped)" in summary
        assert f"- #{n['b']} B — skipped in the epic run, then closed on the forge" in summary

    def test_a_summary_already_posted_is_not_posted_again(self, tmp_path: Path) -> None:
        config, fake = _config(tmp_path), FakeGithub()
        store, n = _published(tmp_path, fake, config, _lone_epic("o/r"))
        _close_on_github(fake, n["a"])
        _close_on_github(fake, n["b"])
        # An earlier look commented and died before it closed the epic.
        fake.issue_comment("o/r", n["e1"], summary_marker("plan_1", "e1") + "\n\nearlier")

        result = _completion(fake, store, config).epic("plan_1", "e1")

        assert result.closed == ["e1"]
        assert len(_summaries(fake, n["e1"])) == 1
        assert fake.issues_closed == [(n["e1"], "completed")]

    def test_an_epic_a_person_closed_is_recorded_without_a_summary(self, tmp_path: Path) -> None:
        config, fake = _config(tmp_path), FakeGithub()
        store, n = _published(tmp_path, fake, config, _lone_epic("o/r"))
        for key in ("a", "b", "e1"):
            _close_on_github(fake, n[key])

        result = _completion(fake, store, config).epic("plan_1", "e1")

        assert result.closed == [] and "e1" in result.recorded
        assert fake.issue_answers == [] and fake.issues_closed == []
        assert _forge_state(store, "e1") == "closed"

    def test_close_completed_off_leaves_the_epic_open(self, tmp_path: Path) -> None:
        config, fake = _config(tmp_path, close_completed=False), FakeGithub()
        store, n = _published(tmp_path, fake, config, _lone_epic("o/r"))
        _close_on_github(fake, n["a"])
        _close_on_github(fake, n["b"])

        result = _completion(fake, store, config).epic("plan_1", "e1")

        assert result.closed == [] and result.open == []
        assert fake.issue_answers == [] and fake.issues_closed == []
        assert (_forge_state(store, "a"), _forge_state(store, "e1")) == ("closed", "open")


class TestInitiative:
    def test_the_last_epic_closing_rolls_up_and_closes_the_initiative(self, tmp_path: Path) -> None:
        config, fake = _config(tmp_path), FakeGithub()
        store, n = _published(tmp_path, fake, config, _initiative("o/r"))
        completion = _completion(fake, store, config)
        _close_on_github(fake, n["a"])
        _close_on_github(fake, n["b"])

        first = completion.epic("plan_1", "e1")
        assert first.closed == ["e1"]
        assert _summaries(fake, n["ini"]) == [] and _forge_state(store, "ini") == "open"

        _close_on_github(fake, n["c"])
        second = completion.epic("plan_1", "e2")
        assert second.closed == ["e2", "ini"]
        (rollup,) = _summaries(fake, n["ini"])
        assert rollup.startswith(summary_marker("plan_1", "ini"))
        assert "Every epic of this initiative is closed" in rollup
        assert f"- #{n['e1']} E1 — closed, 2 tasks" in rollup
        assert f"- #{n['e2']} E2 — closed, 1 task" in rollup
        assert fake.issues_closed == [
            (n["e1"], "completed"),
            (n["e2"], "completed"),
            (n["ini"], "completed"),
        ]
        assert _forge_state(store, "ini") == "closed"

        completion.epic("plan_1", "e2")
        assert len(_summaries(fake, n["ini"])) == 1

    def test_close_completed_off_for_the_initiatives_repository_leaves_it_open(
        self, tmp_path: Path
    ) -> None:
        config = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "vcs": {
                    "repos": [
                        {"repo": "o/r", "planning": {"close_completed": False}},
                        {"repo": "o/other"},
                    ]
                },
            }
        )
        fake = FakeGithub()
        nodes = _initiative("o/r")
        # The epic lives elsewhere, where closing is on.
        nodes[1:] = [
            _node("e1", level="epic", parent_id="ini", repository="o/other"),
            _node("a", parent_id="e1", repository="o/other", kind="code"),
        ]
        store, n = _published(tmp_path, fake, config, nodes)
        _close_on_github(fake, n["a"])

        result = _completion(fake, store, config).epic("plan_1", "e1")

        assert result.closed == ["e1"]
        assert _summaries(fake, n["ini"]) == []
        assert fake.issues_closed == [(n["e1"], "completed")]


class TestGitlabChecklist:
    def test_each_closed_task_is_ticked_and_the_last_closes_the_epic(self, tmp_path: Path) -> None:
        config, fake = _config(tmp_path, kind="gitlab"), FakeGitlab()
        store, n = _published(tmp_path, fake, config, _lone_epic("acme/widgets"))

        def ticks() -> dict[str, bool]:
            body = str(fake.issues[n["e1"]]["description"])
            return {entry.ref: entry.closed for entry in parse_checklist(body)}

        a, b = f"acme/widgets#{n['a']}", f"acme/widgets#{n['b']}"
        assert ticks() == {a: False, b: False}

        fake.issues[n["a"]]["state"] = "closed"
        first = _completion(fake, store, config).epic("plan_1", "e1")
        assert first.ticked == ["a"] and first.closed == []
        assert ticks() == {a: True, b: False}
        assert fake.issues_closed == []

        fake.issues[n["b"]]["state"] = "closed"
        second = _completion(fake, store, config).epic("plan_1", "e1")
        assert second.ticked == ["b"] and second.closed == ["e1"]
        assert ticks() == {a: True, b: True}
        assert fake.issues_closed == [n["e1"]]
        notes = [note["body"] for note in fake.notes[n["e1"]]]
        assert sum(summary_marker("plan_1", "e1") in body for body in notes) == 1

        # Nothing left to tick or close: the description is not rewritten.
        before = fake.issues[n["e1"]]["description"]
        third = _completion(fake, store, config).epic("plan_1", "e1")
        assert third.ticked == [] and third.closed == []
        assert fake.issues[n["e1"]]["description"] == before

    def test_ticks_follow_the_forge_even_with_close_completed_off(self, tmp_path: Path) -> None:
        config = _config(tmp_path, kind="gitlab", close_completed=False)
        fake = FakeGitlab()
        store, n = _published(tmp_path, fake, config, _lone_epic("acme/widgets"))
        fake.issues[n["a"]]["state"] = "closed"
        fake.issues[n["b"]]["state"] = "closed"

        result = _completion(fake, store, config).epic("plan_1", "e1")

        assert result.ticked == ["a", "b"] and result.closed == []
        assert fake.issues_closed == []
        assert all(e.closed for e in parse_checklist(str(fake.issues[n["e1"]]["description"])))
