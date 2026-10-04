"""Goals in the daemon's store: written, edited against the revision the
caller read, listed by repository and state, deleted — and the plans that
serve each, read from the plans that name it."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lantern.daemon.goals import (
    TEXT_MAX,
    TITLE_MAX,
    Goal,
    GoalGone,
    GoalInvalid,
    GoalStore,
    StaleGoal,
    open_plan,
)
from lantern.daemon.store import DaemonStore
from lantern.paths import LanternHome


@pytest.fixture
def dstore(tmp_path: Path) -> Iterator[DaemonStore]:
    store = DaemonStore(LanternHome(tmp_path).state_db)
    yield store
    store.close()


@pytest.fixture
def store(dstore: DaemonStore) -> GoalStore:
    return GoalStore(dstore)


def make(store: GoalStore, now: float = 10.0, **fields: Any) -> Goal:
    values: dict[str, Any] = {
        "repository": "o/r",
        "title": "Faster builds",
        "text": "Cut the build time in half without dropping a check.",
        "created_by": "usr_owner",
        "created_by_display": "owner",
        "now": now,
        **fields,
    }
    return store.create(**values)


def _plan(
    dstore: DaemonStore,
    plan_id: str,
    goal_id: str | None,
    *,
    title: str,
    updated_at: float,
    state: str = "active",
    node_state: str = "draft",
    advance: str = "manual",
) -> None:
    """A plan row and its root node, as the plan store writes them."""
    with sqlite3.connect(dstore.path) as db:
        db.execute(
            "INSERT INTO daemon_plans (plan_id, workspace_id, root_node_id, state, created_at, "
            "updated_at, revision, advance, goal_id) VALUES (?, 'default', ?, ?, 1, ?, 1, ?, ?)",
            (plan_id, f"{plan_id}_root", state, updated_at, advance, goal_id),
        )
        db.execute(
            "INSERT INTO daemon_plan_nodes (node_id, plan_id, position, level, repository, "
            "state, origin, title, created_at, updated_at) VALUES "
            "(?, ?, 0, 'epic', 'o/r', ?, 'planner', ?, 1, 1)",
            (f"{plan_id}_root", plan_id, node_state, title),
        )


class TestGoals:
    def test_a_goal_is_written_at_revision_one(self, store: GoalStore) -> None:
        goal = make(store)
        assert goal.id.startswith("goal_") and goal.revision == 1
        assert goal.state == "active" and goal.created_at == goal.updated_at == 10.0
        assert store.goal(goal.id) == goal
        assert store.goal("goal_missing") is None

    def test_an_edit_names_the_revision_it_read(self, store: GoalStore) -> None:
        goal = make(store)
        edited = store.update(
            goal.id, {"state": "paused", "title": "Quicker builds"}, expected_revision=1, now=20.0
        )
        assert (edited.state, edited.title, edited.revision) == ("paused", "Quicker builds", 2)
        assert edited.updated_at == 20.0 and edited.created_at == 10.0
        with pytest.raises(StaleGoal) as stale:
            store.update(goal.id, {"state": "done"}, expected_revision=1, now=30.0)
        assert stale.value.current == 2
        assert store.goal(goal.id) == edited

    def test_an_edit_refuses_what_it_cannot_change(self, store: GoalStore) -> None:
        goal = make(store)
        with pytest.raises(GoalInvalid) as refused:
            store.update(goal.id, {"repository": "o/other"}, expected_revision=1, now=20.0)
        assert refused.value.field == "repository"
        with pytest.raises(GoalInvalid) as bad_state:
            store.update(goal.id, {"state": "finished"}, expected_revision=1, now=20.0)
        assert bad_state.value.field == "state"
        with pytest.raises(GoalGone):
            store.update("goal_missing", {"state": "done"}, expected_revision=1, now=20.0)
        assert store.goal(goal.id) == goal

    @pytest.mark.parametrize(
        ("fields", "field"),
        [
            ({"title": "  "}, "title"),
            ({"title": "x" * (TITLE_MAX + 1)}, "title"),
            ({"text": ""}, "text"),
            ({"text": "x" * (TEXT_MAX + 1)}, "text"),
            ({"state": "someday"}, "state"),
        ],
    )
    def test_a_goal_out_of_bounds_is_refused_by_name(
        self, store: GoalStore, fields: dict[str, Any], field: str
    ) -> None:
        with pytest.raises(GoalInvalid) as refused:
            make(store, **fields)
        assert refused.value.field == field
        assert store.goals() == []

    def test_goals_list_oldest_first_by_repository_and_state(self, store: GoalStore) -> None:
        first = make(store, now=1.0)
        second = make(store, now=2.0, repository="O/Other", state="paused")
        third = make(store, now=3.0, state="done")
        assert store.goals() == [first, second, third]
        assert store.goals(repository="o/r") == [first, third]
        assert store.goals(repository="o/other") == [second]
        assert store.goals(state="paused") == [second]
        assert store.goals(repository="o/r", state="done") == [third]

    def test_a_deleted_goal_is_gone_and_said_once(self, store: GoalStore) -> None:
        goal = make(store)
        assert store.delete(goal.id) == goal
        assert store.goal(goal.id) is None and store.delete(goal.id) is None


class TestPlansServingAGoal:
    def test_the_plans_that_name_a_goal_most_recent_first(
        self, dstore: DaemonStore, store: GoalStore
    ) -> None:
        goal = make(store)
        other = make(store)
        _plan(dstore, "plan_old", goal.id, title="First try", updated_at=1.0, state="archived")
        _plan(
            dstore,
            "plan_new",
            goal.id,
            title="Second try",
            updated_at=5.0,
            node_state="published",
            advance="auto",
        )
        _plan(dstore, "plan_draft", goal.id, title="Third", updated_at=3.0)
        _plan(dstore, "plan_other", other.id, title="Elsewhere", updated_at=2.0)
        _plan(dstore, "plan_person", None, title="A person's", updated_at=4.0)

        plans = store.plans_for(goal.id)

        assert [(p.plan_id, p.title, p.state, p.advance) for p in plans] == [
            ("plan_new", "Second try", "published", "auto"),
            ("plan_draft", "Third", "draft", "manual"),
            ("plan_old", "First try", "archived", "manual"),
        ]
        serving = open_plan(plans)
        assert serving is not None and serving.plan_id == "plan_new"
        by_goal = store.plans_by_goal([goal.id, other.id, "goal_none"])
        assert set(by_goal) == {goal.id, other.id}
        assert [p.plan_id for p in by_goal[other.id]] == ["plan_other"]
        assert store.plans_by_goal([]) == {}

    def test_a_goal_with_only_archived_plans_has_none_open(
        self, dstore: DaemonStore, store: GoalStore
    ) -> None:
        goal = make(store)
        assert store.plans_for(goal.id) == [] and open_plan([]) is None
        _plan(dstore, "plan_done", goal.id, title="Done", updated_at=1.0, state="archived")
        assert open_plan(store.plans_for(goal.id)) is None
