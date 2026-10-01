"""Publishing one plan level to the forge, idempotent by marker (#2341).

A published node is one issue: its sections rendered as the body, the
``sbx-plan`` marker at the foot, the level label and never the trigger or
workload label, linked under its parent (a GitHub sub-issue, or a line in
the parent's managed checklist on GitLab). Each node is recorded as it
lands, so an interrupted level resumes — finding by marker what an earlier
attempt created — and never duplicates an issue.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lantern.config import Config
from lantern.daemon.store import DaemonStore
from lantern.errors import GithubOpsError
from lantern.plans.model import Plan, PlanNode
from lantern.plans.publish import level_targets, publish_level
from lantern.plans.render import marker
from lantern.plans.store import PlanStore
from lantern.vcs.checklist import END, START, parse_checklist
from tests.fakes.fake_github import FakeGithub
from tests.fakes.fake_gitlab import FakeGitlab

NOW = 1_800_000_000.0


def _github(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "vcs": {"repos": [{"repo": "o/r"}, {"repo": "o/other"}]},
        }
    )


def _gitlab(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "vcs": {"kind": "gitlab", "repos": [{"repo": "acme/widgets"}]},
        }
    )


def _node(plan_id: str, node_id: str, **fields: Any) -> PlanNode:
    base: dict[str, Any] = {
        "id": node_id,
        "plan_id": plan_id,
        "parent_id": None,
        "position": 0,
        "level": "epic",
        "repository": "o/r",
        "state": "approved",
        "origin": "person",
        "title": node_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(fields)
    return PlanNode(**base)


def _store(tmp_path: Path) -> PlanStore:
    return PlanStore(DaemonStore(tmp_path / "state.db"))


def _save(store: PlanStore, nodes: list[PlanNode]) -> Plan:
    root = nodes[0]
    plan = Plan(
        id=root.plan_id,
        workspace_id="default",
        root_id=root.id,
        archived=False,
        created_by=None,
        created_by_display=None,
        created_at=NOW,
        updated_at=NOW,
        revision=1,
        nodes=tuple(nodes),
    )
    return store.create(plan, events=[], actor=None)


def _epic(repo: str = "o/r") -> list[PlanNode]:
    """A lone epic with two approved tasks (B after A) and a draft one."""
    task = {"parent_id": "epic", "level": "task", "repository": repo, "kind": "code"}
    return [
        _node("plan_1", "epic", repository=repo, state="draft", goal="Ship it"),
        _node("plan_1", "a", position=0, acceptance_criteria=("stored",), **task),
        _node("plan_1", "b", position=1, depends_on=("a",), verify_commands=("make t",), **task),
        _node("plan_1", "c", position=2, **{**task, "state": "draft"}),
    ]


def _publish(ops: Any, store: PlanStore, config: Config, node_id: str = "epic") -> Any:
    plan = store.get("plan_1")
    assert plan is not None
    return publish_level(
        ops, store=store, config=config, plan=plan, node_id=node_id, clock=lambda: NOW, actor=None
    )


def _labels(fake: FakeGithub) -> set[str]:
    return {label for _, _, labels in fake.issues_created for label in labels}


class TestWhatIsPublished:
    def test_the_root_first_then_its_approved_children_in_dependency_order(self) -> None:
        nodes = _epic()
        # B listed first among siblings, still after A which it depends on.
        nodes[1], nodes[2] = replace(nodes[1], position=1), replace(nodes[2], position=0)
        plan = Plan(
            id="plan_1",
            workspace_id="default",
            root_id="epic",
            archived=False,
            created_by=None,
            created_by_display=None,
            created_at=NOW,
            updated_at=NOW,
            revision=1,
            nodes=tuple(nodes),
        )
        assert [n.id for n in level_targets(plan, "epic")] == ["epic", "a", "b"]
        published = replace(plan, nodes=(replace(nodes[0], state="published"), *nodes[1:]))
        assert [n.id for n in level_targets(published, "epic")] == ["a", "b"]


class TestGithub:
    def test_a_level_becomes_labelled_sub_issues_with_the_marker(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        _save(store, _epic())
        result = _publish(fake, store, config)
        assert [(r.node_id, r.outcome, r.linked) for r in result.results] == [
            ("epic", "created", "none"),
            ("a", "created", "native"),
            ("b", "created", "native"),
        ]
        titles = [title for title, _, _ in fake.issues_created]
        assert titles == ["epic", "a", "b"]
        bodies = {title: body for title, body, _ in fake.issues_created}
        assert bodies["a"].rstrip().endswith(marker("plan_1", "a"))
        numbers = {r.node_id: r.number for r in result.results}
        assert f"## Depends on\n\n- #{numbers['a']}" in bodies["b"]
        assert [labels for _, _, labels in fake.issues_created] == [
            ["sbx:epic"],
            ["sbx:task"],
            ["sbx:task"],
        ]
        assert fake.sub_issues[("o/r", numbers["epic"])] == [
            ("o/r", numbers["a"]),
            ("o/r", numbers["b"]),
        ]
        plan = result.plan
        states = {n.id: (n.state, n.forge.number if n.forge else None) for n in plan.nodes}
        assert states == {
            "epic": ("published", numbers["epic"]),
            "a": ("published", numbers["a"]),
            "b": ("published", numbers["b"]),
            "c": ("draft", None),
        }

    def test_the_trigger_and_workload_labels_are_never_applied(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        nodes = _epic()
        nodes[3] = replace(nodes[3], state="approved", kind="workload")
        _save(store, nodes)
        _publish(fake, store, config)
        lifecycle = config.labels_for("o/r")
        assert lifecycle.trigger not in _labels(fake) and lifecycle.workload not in _labels(fake)
        assert _labels(fake) == {"sbx:epic", "sbx:task"}
        # Nothing else put a label on any issue afterwards.
        assert not [
            c
            for c in fake.raw_calls
            if c[0] == "POST" and c[1].endswith("/labels") and c[2] and "labels" in c[2]
        ]

    def test_each_level_label_is_made_sure_of_once(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        _save(store, _epic())
        _publish(fake, store, config)
        assert sorted(fake.label_creates) == ["sbx:epic", "sbx:task"]

    def test_publishing_again_creates_nothing(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        _save(store, _epic())
        _publish(fake, store, config)
        again = _publish(fake, store, config)
        assert again.results == ()
        assert len(fake.issues_created) == 3

    def test_an_interrupted_level_resumes_by_marker_without_duplicating(
        self, tmp_path: Path
    ) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        _save(store, _epic())
        # A's issue is created, then linking it under the epic fails: the
        # attempt stops short of recording it, and B, which depends on A,
        # is not attempted.
        fake.fail_once["sub_issue_add"] = GithubOpsError("secondary rate limit", http_status=403)
        first = _publish(fake, store, config)
        outcomes = {r.node_id: (r.outcome, r.error) for r in first.results}
        assert outcomes["epic"] == ("created", None)
        assert outcomes["a"][0] == "failed" and "secondary rate limit" in str(outcomes["a"][1])
        assert outcomes["b"][0] == "failed" and "depends on a" in str(outcomes["b"][1])
        assert {n.id: n.state for n in first.plan.nodes}["a"] == "approved"
        assert [t for t, _, _ in fake.issues_created] == ["epic", "a"]

        second = _publish(fake, store, config)
        assert [(r.node_id, r.outcome, r.linked) for r in second.results] == [
            ("a", "found", "native"),
            ("b", "created", "native"),
        ]
        assert [t for t, _, _ in fake.issues_created] == ["epic", "a", "b"]
        epic = next(r.number for r in first.results if r.node_id == "epic")
        assert len(fake.sub_issues[("o/r", epic)]) == 2

    def test_a_crash_after_the_issue_is_created_resumes_without_duplicating(
        self, tmp_path: Path
    ) -> None:
        config, fake = _github(tmp_path), FakeGithub()

        class Crashing(PlanStore):
            crashed = False

            def apply(self, plan_id: str, **kwargs: Any) -> Plan:
                upsert = kwargs.get("upsert") or ()
                if not self.crashed and any(n.id == "a" for n in upsert):
                    self.crashed = True
                    raise RuntimeError("the daemon died")
                return super().apply(plan_id, **kwargs)

        store = Crashing(DaemonStore(tmp_path / "state.db"))
        _save(store, _epic())
        with pytest.raises(RuntimeError):
            _publish(fake, store, config)
        resumed = _publish(fake, store, config)
        assert [(r.node_id, r.outcome) for r in resumed.results] == [
            ("a", "found"),
            ("b", "created"),
        ]
        assert [t for t, _, _ in fake.issues_created] == ["epic", "a", "b"]

    def test_a_link_that_already_landed_is_not_made_twice(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        _save(store, _epic())

        class Forgetful(PlanStore):
            forgot = False

            def apply(self, plan_id: str, **kwargs: Any) -> Plan:
                if not self.forgot and any(n.id == "a" for n in kwargs.get("upsert") or ()):
                    self.forgot = True
                    raise RuntimeError("the daemon died after linking")
                return super().apply(plan_id, **kwargs)

        store = Forgetful(store.dstore)
        with pytest.raises(RuntimeError):
            _publish(fake, store, config)
        # GitHub answers a second link of the same child with a 422; the
        # resumed walk sees the child is already there and does not ask.
        resumed = _publish(fake, store, config)
        assert resumed.results[0].linked == "native" and resumed.results[0].error is None
        assert not fake.failed_jobs

    def test_a_refused_cross_repository_link_falls_back_to_the_checklist(
        self, tmp_path: Path
    ) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        fake.refuse_cross_repo_sub_issues = True
        _save(
            store,
            [
                _node("plan_1", "init", level="initiative", state="draft"),
                _node("plan_1", "home", parent_id="init", position=0),
                _node("plan_1", "away", parent_id="init", position=1, repository="o/other"),
            ],
        )
        result = _publish(fake, store, config, "init")
        by_id = {r.node_id: r for r in result.results}
        assert by_id["home"].linked == "native" and by_id["home"].reason is None
        away = by_id["away"]
        assert away.outcome == "created" and away.linked == "checklist"
        assert away.reason and "cross-repository" in away.reason
        root = by_id["init"].number
        body = str(fake.issue_get("o/r", root)["body"])
        assert [e.ref for e in parse_checklist(body)] == [f"o/other#{away.number}"]
        assert {n.id: n.state for n in result.plan.nodes}["away"] == "published"


class TestGitlab:
    def test_the_parent_lists_its_children_and_nothing_else_of_it_changes(
        self, tmp_path: Path
    ) -> None:
        config, store, fake = _gitlab(tmp_path), _store(tmp_path), FakeGitlab()
        _save(store, _epic("acme/widgets"))
        result = _publish(fake, store, config)
        assert [(r.node_id, r.outcome, r.linked) for r in result.results] == [
            ("epic", "created", "none"),
            ("a", "created", "checklist"),
            ("b", "created", "checklist"),
        ]
        numbers = {r.node_id: r.number for r in result.results}
        rendered = fake.issues_created[0][1]
        description = str(fake.issues[numbers["epic"]]["description"])
        assert description.startswith(rendered.rstrip("\n"))
        assert description.count(START) == 1 and description.rstrip().endswith(END)
        assert [(e.ref, e.title) for e in parse_checklist(description)] == [
            (f"acme/widgets#{numbers['a']}", "a"),
            (f"acme/widgets#{numbers['b']}", "b"),
        ]
        assert [labels for _, _, labels in fake.issues_created] == [
            ["sbx:epic"],
            ["sbx:task"],
            ["sbx:task"],
        ]

    def test_publishing_again_leaves_the_checklist_alone(self, tmp_path: Path) -> None:
        config, store, fake = _gitlab(tmp_path), _store(tmp_path), FakeGitlab()
        _save(store, _epic("acme/widgets"))
        first = _publish(fake, store, config)
        epic = first.results[0].number
        before = fake.issues[epic]["description"]
        again = _publish(fake, store, config)
        assert again.results == () and fake.issues[epic]["description"] == before

    def test_a_mangled_checklist_fails_the_child_and_is_left_as_it_is(self, tmp_path: Path) -> None:
        config, store, fake = _gitlab(tmp_path), _store(tmp_path), FakeGitlab()
        _save(store, _epic("acme/widgets"))
        fake.fail_once["issue_create"] = GithubOpsError("boom", http_status=502)
        first = _publish(fake, store, config)
        assert all(r.outcome == "failed" for r in first.results)
        assert "parent" in str(first.results[1].error)
        second = _publish(fake, store, config)
        epic = second.results[0].number
        assert epic is not None
        mangled = str(fake.issues[epic]["description"]) + f"\n{START}\nnot a child\n"
        fake.issues[epic]["description"] = mangled
        # A third publish after a new task is approved cannot list it.
        plan = store.get("plan_1")
        assert plan is not None
        c = plan.node("c")
        assert c is not None
        store.apply(
            "plan_1",
            expected_revision=plan.revision,
            now=NOW,
            upsert=[replace(c, state="approved")],
        )
        third = _publish(fake, store, config)
        (only,) = third.results
        assert only.outcome == "failed" and "sbx-plan:children" in str(only.error)
        assert fake.issues[epic]["description"] == mangled
