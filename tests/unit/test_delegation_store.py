"""Grants and the decisions ledger in the daemon's store, and the revision
that brings a deployed database to them."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lantern.daemon.controls.delegation import Conditions, Decision, Grant, decide
from lantern.daemon.controls.delegation_store import (
    DecisionRecord,
    DelegationStore,
    GrantGone,
    StaleGrant,
)
from lantern.daemon.loop import day_window
from lantern.daemon.store import DaemonStore
from lantern.paths import LanternHome
from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DelegationStore]:
    dstore = DaemonStore(LanternHome(tmp_path).state_db)
    yield DelegationStore(dstore)
    dstore.close()


def make(store: DelegationStore, now: float = 10.0, **fields: Any) -> Grant:
    values: dict[str, Any] = {
        "agent_slug": "critic",
        "action": "plan.approve",
        "conditions": Conditions(max_children=8, require_review=True),
        "daily_limit": 5,
        "enabled": True,
        "note": "small levels only",
        "created_by": "usr_owner",
        "created_by_display": "Olive Owner",
    }
    values.update(fields)
    return store.create_grant(now=now, **values)


def record(
    store: DelegationStore,
    outcome: str,
    at: float,
    *,
    grant_id: str | None = None,
    agent: str = "critic",
    **refs: Any,
) -> DecisionRecord:
    return store.record(
        Decision(outcome=outcome, grant_id=grant_id, reason=f"{outcome} at {at}"),  # type: ignore[arg-type]
        agent_slug=agent,
        action="plan.approve",
        attrs={"repository": "o/r", "level": "epic", "child_count": 3},
        now=at,
        **refs,
    )


class TestGrants:
    def test_a_grant_is_stored_as_it_was_written(self, store: DelegationStore) -> None:
        created = make(store)
        assert created.id.startswith("grant_") and created.revision == 1
        assert created.created_at == created.updated_at == 10.0
        assert created.conditions == Conditions(max_children=8, require_review=True)
        assert store.grant(created.id) == created
        assert store.grants() == [created]
        assert store.grant("grant_nope") is None

    def test_grants_are_listed_oldest_first_and_filtered(self, store: DelegationStore) -> None:
        second = make(
            store, now=20.0, agent_slug="planner", action="plan.breakdown", conditions=Conditions()
        )
        first = make(store, now=10.0)
        off = make(store, now=30.0, enabled=False)
        assert [g.id for g in store.grants()] == [first.id, second.id, off.id]
        assert [g.id for g in store.grants(enabled_only=True)] == [first.id, second.id]
        assert [g.id for g in store.grants(agent_slug="planner")] == [second.id]
        assert [g.id for g in store.grants(action="plan.approve")] == [first.id, off.id]

    def test_an_edit_names_the_revision_it_read(self, store: DelegationStore) -> None:
        created = make(store)
        edited = store.update_grant(
            created.id,
            {"daily_limit": None, "enabled": False, "conditions": Conditions(levels=("task",))},
            expected_revision=1,
            now=20.0,
        )
        assert edited.revision == 2 and edited.updated_at == 20.0 and edited.created_at == 10.0
        assert edited.daily_limit is None and not edited.enabled
        assert edited.conditions == Conditions(levels=("task",))
        assert edited.note == "small levels only" and edited.created_by == "usr_owner"
        assert store.grant(created.id) == edited

    def test_a_stale_edit_is_refused_and_changes_nothing(self, store: DelegationStore) -> None:
        created = make(store)
        store.update_grant(created.id, {"note": "first"}, expected_revision=1, now=20.0)
        with pytest.raises(StaleGrant) as stale:
            store.update_grant(created.id, {"note": "second"}, expected_revision=1, now=30.0)
        assert stale.value.current == 2
        current = store.grant(created.id)
        assert current is not None and current.note == "first" and current.revision == 2

    def test_an_edit_of_what_is_not_there_says_so(self, store: DelegationStore) -> None:
        with pytest.raises(GrantGone):
            store.update_grant("grant_nope", {"note": "x"}, expected_revision=1, now=1.0)

    def test_only_the_editable_fields_can_be_edited(self, store: DelegationStore) -> None:
        created = make(store)
        for field in ("agent_slug", "action", "id", "created_by", "revision"):
            with pytest.raises(ValueError, match=field):
                store.update_grant(created.id, {field: "x"}, expected_revision=1, now=2.0)

    def test_a_grant_is_deleted_once(self, store: DelegationStore) -> None:
        created = make(store)
        allowed = record(store, "allow", 11.0, grant_id=created.id)
        removed = store.delete_grant(created.id)
        assert removed == created
        assert store.grant(created.id) is None and store.grants() == []
        assert store.delete_grant(created.id) is None
        # The ledger keeps what the grant allowed.
        assert store.decision(allowed.id) == allowed


class TestDecisions:
    def test_a_decision_is_recorded_with_what_was_judged(self, store: DelegationStore) -> None:
        grant = make(store)
        attrs = {
            "repository": "o/r",
            "level": "epic",
            "child_count": 3,
            "proposer": "planner",
            "review_verdict": "approve",
        }
        decision = decide(
            store.grants(), agent_slug="critic", action="plan.approve", attrs=attrs, used_today={}
        )
        row = store.record(
            decision,
            agent_slug="critic",
            action="plan.approve",
            attrs=attrs,
            now=50.0,
            plan_id="plan_1",
            node_id="node_1",
            operation_id="op_1",
        )
        assert row.id.startswith("dec_")
        assert (row.outcome, row.grant_id, row.reason) == ("allow", grant.id, decision.reason)
        assert (row.agent_slug, row.action, row.at) == ("critic", "plan.approve", 50.0)
        assert (row.plan_id, row.node_id, row.operation_id) == ("plan_1", "node_1", "op_1")
        assert (row.item_id, row.run_id, row.epic_run_id) == (None, None, None)
        assert row.repository == "o/r" and row.attrs == attrs
        assert (row.resolved_at, row.resolved_by, row.resolution) == (None, None, None)
        assert store.decision(row.id) == row
        assert store.decision("dec_nope") is None

    def test_the_repository_can_be_named_apart_from_the_facts(self, store: DelegationStore) -> None:
        row = store.record(
            Decision(outcome="escalate", reason="no grant"),
            agent_slug="operator",
            action="item.retry",
            attrs={},
            now=1.0,
            item_id="gh:issue:7",
            run_id="r1234abcd",
            epic_run_id="erun_1",
            repository="o/two",
        )
        assert (row.repository, row.item_id, row.run_id, row.epic_run_id) == (
            "o/two",
            "gh:issue:7",
            "r1234abcd",
            "erun_1",
        )

    def test_the_ledger_pages_newest_first(self, store: DelegationStore) -> None:
        rows = [record(store, "allow", float(at), grant_id="grant_a") for at in (1, 2, 3, 3, 4)]
        ordered = sorted(rows, key=lambda r: (r.at, r.id), reverse=True)
        first = store.page(limit=2)
        assert [r.id for r in first] == [r.id for r in ordered[:2]]
        second = store.page(limit=2, after=(first[-1].at, first[-1].id))
        assert [r.id for r in second] == [r.id for r in ordered[2:4]]
        last = store.page(limit=2, after=(second[-1].at, second[-1].id))
        assert [r.id for r in last] == [ordered[4].id]

    def test_the_ledger_filters(self, store: DelegationStore) -> None:
        allowed = record(store, "allow", 1.0, grant_id="grant_a")
        denied = record(store, "deny", 2.0)
        waiting = record(store, "escalate", 3.0, agent="operator")
        settled = record(store, "escalate", 4.0)
        store.resolve(settled.id, by="usr_owner", resolution="declined", now=5.0)

        def ids(**filters: Any) -> list[str]:
            return [r.id for r in store.page(**filters)]

        assert ids() == [settled.id, waiting.id, denied.id, allowed.id]
        assert ids(outcome="allow") == [allowed.id]
        assert ids(outcome="deny") == [denied.id]
        assert ids(outcome="escalate") == [settled.id, waiting.id]
        assert ids(unresolved=True) == [waiting.id]
        assert ids(agent_slug="operator") == [waiting.id]
        assert ids(agent_slug="Critic") == [settled.id, denied.id, allowed.id]
        assert ids(since=3.0) == [settled.id, waiting.id]

    def test_what_a_grant_allowed_today_is_counted_from_the_ledger(
        self, store: DelegationStore
    ) -> None:
        now = 1_700_000_000.0
        day_start, next_start = day_window(now, "UTC")
        record(store, "allow", day_start - 1, grant_id="grant_a")  # yesterday
        record(store, "allow", day_start, grant_id="grant_a")
        record(store, "allow", now, grant_id="grant_a")
        record(store, "allow", now, grant_id="grant_b")
        record(store, "escalate", now)
        record(store, "deny", now)
        assert store.used_today(day_start) == {"grant_a": 2, "grant_b": 1}
        assert store.used_today(next_start) == {}

    def test_an_escalation_is_resolved_once(self, store: DelegationStore) -> None:
        waiting = record(store, "escalate", 3.0)
        resolved = store.resolve(waiting.id, by="usr_owner", resolution="acted", now=9.0)
        assert resolved is not None
        assert (resolved.resolved_at, resolved.resolved_by, resolved.resolution) == (
            9.0,
            "usr_owner",
            "acted",
        )
        # The first resolution stands.
        again = store.resolve(waiting.id, by="usr_other", resolution="declined", now=12.0)
        assert again == resolved and store.decision(waiting.id) == resolved

    def test_only_an_escalation_can_be_resolved(self, store: DelegationStore) -> None:
        allowed = record(store, "allow", 1.0, grant_id="grant_a")
        assert store.resolve(allowed.id, by="usr_owner", resolution="acted", now=2.0) is None
        assert store.resolve("dec_nope", by="usr_owner", resolution="acted", now=2.0) is None
        unchanged = store.decision(allowed.id)
        assert unchanged is not None and unchanged.resolved_at is None


class TestTheMigration:
    def test_a_deployed_database_gains_both_tables_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        _upgrade_to(path, "0049")
        names = {name for (name,) in _query(path, "SELECT name FROM sqlite_master")}
        assert not {"daemon_grants", "daemon_decisions"} & names
        _head(path)
        names = {name for (name,) in _query(path, "SELECT name FROM sqlite_master")}
        assert {"daemon_grants", "daemon_decisions"} <= names
        # Grants ship empty: an upgrade delegates nothing.
        assert _query(path, "SELECT COUNT(*) FROM daemon_grants") == [(0,)]
        assert _query(path, "SELECT COUNT(*) FROM daemon_decisions") == [(0,)]

    def test_it_runs_again_on_a_rewound_stamp(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        _head(path)
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO daemon_grants (grant_id, agent_slug, action, created_at, updated_at) "
                "VALUES ('grant_a', 'critic', 'plan.approve', 1, 1)"
            )
            db.execute(
                "INSERT INTO daemon_decisions (decision_id, agent_slug, action, outcome, reason, "
                "at) VALUES ('dec_a', 'critic', 'plan.approve', 'escalate', 'no grant', 2)"
            )
        _stamp(path, "0049")
        _head(path)
        assert _query(
            path, "SELECT grant_id, conditions_json, enabled, revision FROM daemon_grants"
        ) == [("grant_a", "{}", 1, 1)]
        assert _query(path, "SELECT decision_id, attrs_json FROM daemon_decisions") == [
            ("dec_a", "{}")
        ]

    def test_a_store_opened_on_the_upgraded_file_reads_and_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        _upgrade_to(path, "0049")
        dstore = DaemonStore(path)
        try:
            store = DelegationStore(dstore)
            created = make(store)
            assert store.grants() == [created]
        finally:
            dstore.close()
