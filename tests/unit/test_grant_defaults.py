"""Lantern's default grants: seeded once when the daemon starts, never over
an owner's edit, never again after an owner deleted one, and brought back
only by a restore — plus the revision that marks which grants are defaults.

The loop is the tests' ``Harness`` (real stores, a scripted runner); the
plan driver is the one its own suite drives (``World``).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from lantern.config import Config
from lantern.daemon.controls.delegation import Grant
from lantern.daemon.controls.delegation_defaults import (
    DEFAULT_GRANTS,
    DEFAULT_KEYS,
    SEEDED_PREFIX,
)
from lantern.daemon.controls.delegation_store import DelegationStore
from lantern.daemon.controls.operations import EFFECTS, Operation, _judge
from lantern.daemon.store import DaemonStore
from lantern.db.daemon_models import GrantRow
from lantern.paths import LanternHome
from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to
from tests.unit.test_daemon_loop import Harness
from tests.unit.test_plan_driver import World
from tests.unit.test_triage import CI_TIMEOUT, _decisions, _fail, _harness, _reasons, _triage

#: The table the owner decided on, written out by hand: what a fresh
#: install must hold, key for key.
TABLE: list[tuple[str, str, dict[str, Any], int]] = [
    ("planner", "plan.breakdown", {"levels": ["epic", "task"]}, 10),
    ("critic", "plan.approve", {"max_children": 8, "require_review": True}, 5),
    ("critic", "plan.publish", {"max_children": 8, "require_review": True}, 5),
    ("critic", "plan.run", {"max_children": 12}, 3),
    ("planner", "plan.propose", {}, 2),
    (
        "operator",
        "item.retry",
        {"causes": ["ci_timeout", "forge_transient", "provider_throttle"], "max_retries": 1},
        5,
    ),
    (
        "operator",
        "run.grant_rounds",
        {"causes": ["review_rounds_exhausted", "ci_rounds_exhausted"], "max_retries": 1},
        3,
    ),
]


def _shape(grants: list[Grant]) -> list[tuple[str, str, dict[str, Any], int | None]]:
    return [(g.agent_slug, g.action, g.conditions.as_dict(), g.daily_limit) for g in grants]


def _defaults(store: DelegationStore) -> list[Grant]:
    found = [g for g in store.grants() if g.source == "default"]
    return sorted(found, key=lambda g: DEFAULT_KEYS.index(g.default_key or ""))


def _owner(h: Harness, **fields: Any) -> Grant:
    from lantern.daemon.controls.delegation import parse_conditions

    values: dict[str, Any] = {
        "agent_slug": "critic",
        "action": "plan.approve",
        "conditions": parse_conditions("plan.approve", {"max_children": 3}),
        "daily_limit": 1,
        "enabled": True,
        "note": "mine",
        "created_by": "usr_owner",
        "created_by_display": "Owner",
        "now": h.clock(),
    }
    values.update(fields)
    return h.loop.delegation.create_grant(**values)


class TestSeeding:
    def test_a_fresh_install_seeds_exactly_the_table(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.loop.delegation.grants() == []
        h.loop.recover()
        seeded = _defaults(h.loop.delegation)
        assert _shape(seeded) == TABLE
        assert len(h.loop.delegation.grants()) == len(TABLE)
        for grant, default in zip(seeded, DEFAULT_GRANTS, strict=True):
            assert grant.enabled is True and grant.revision == 1
            assert grant.source == "default" and grant.default_key == default.key
            assert grant.created_by == "lantern"
            assert grant.created_by_display == "Lantern default"
            assert grant.note and grant.note.startswith("Lantern default:")
        assert h.loop.delegation.seeded_default_keys() == set(DEFAULT_KEYS)

    def test_the_keys_are_stable_names(self) -> None:
        assert DEFAULT_KEYS == (
            "plan.breakdown:planner:v1",
            "plan.approve:critic:v1",
            "plan.publish:critic:v1",
            "plan.run:critic:v1",
            "plan.propose:planner:v1",
            "item.retry:operator:v1",
            "run.grant_rounds:operator:v1",
        )

    def test_each_default_is_one_an_owner_could_write(self, tmp_path: Path) -> None:
        """The service's own checks accept every default: the action, the
        condition keys it takes, and an agent that can act."""
        from lantern.daemon.controls import ControlService, Principal

        h = Harness(tmp_path)
        service = ControlService(h.loop)
        owner = Principal.trusted("Owner", "test")
        for default in DEFAULT_GRANTS:
            outcome = service.add_grant(
                owner,
                agent_slug=default.agent_slug,
                action=default.action,
                conditions=default.conditions,
                daily_limit=default.daily_limit,
                note=default.note,
            )
            assert outcome.grant_id

    def test_seeding_again_seeds_nothing(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        first = h.loop.seed_default_grants()
        assert len(first) == len(TABLE)
        assert h.loop.seed_default_grants() == []
        h.loop.recover()
        assert h.loop.delegation.grants() == sorted(first, key=lambda g: (g.created_at, g.id))

    def test_seeding_writes_no_chronology_and_a_restore_is_narrated(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        seen: list[tuple[str, dict[str, Any]]] = []
        notice = h.loop._notice

        def spy(kind: str, text: str, **fields: Any) -> None:
            seen.append((kind, fields))
            notice(kind, text, **fields)  # type: ignore[arg-type]

        h.loop._notice = spy  # type: ignore[method-assign]
        h.loop.recover()
        assert [kind for kind, _ in seen if "grant" in kind] == []
        gone = _defaults(h.loop.delegation)[0]
        h.loop.delegation.delete_grant(gone.id)
        h.loop.restore_default_grants(by="Owner")
        (restored,) = [fields for kind, fields in seen if kind == "daemon.grants_restored"]
        assert restored["defaults"] == [gone.default_key] and restored["by"] == "Owner"

    def test_a_deleted_default_is_not_seeded_again_but_restore_brings_it_back(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        gone = next(g for g in _defaults(h.loop.delegation) if g.action == "item.retry")
        h.loop.remove_grant(gone.id, by="Owner")
        # A restart: the key was seeded, so the deleted grant stays deleted.
        h.loop.recover()
        assert all(g.default_key != gone.default_key for g in h.loop.delegation.grants())
        assert h.loop.seed_default_grants() == []

        written, message = h.loop.restore_default_grants(by="Owner")
        (back,) = written
        assert back.default_key == gone.default_key and back.id != gone.id
        assert back.source == "default" and back.enabled is True
        assert _shape([back]) == _shape([gone])
        assert "restored" in message and back.id in message
        assert _shape(_defaults(h.loop.delegation)) == TABLE
        # And it is not written twice.
        assert h.loop.restore_default_grants(by="Owner")[0] == []

    def test_an_edited_or_paused_default_is_untouched_by_restart_and_restore(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        seeded = _defaults(h.loop.delegation)
        edited, paused = seeded[0], seeded[1]
        h.clock.t += 5
        h.loop.delegation.update_grant(
            edited.id, {"daily_limit": 1, "note": "tuned"}, expected_revision=1, now=h.clock()
        )
        h.loop.delegation.update_grant(
            paused.id, {"enabled": False}, expected_revision=1, now=h.clock()
        )
        before = h.loop.delegation.grants()

        h.loop.recover()
        assert h.loop.restore_default_grants(by="Owner")[0] == []

        assert h.loop.delegation.grants() == before
        after_edit = h.loop.delegation.grant(edited.id)
        assert after_edit is not None and after_edit.daily_limit == 1 and after_edit.revision == 2
        after_pause = h.loop.delegation.grant(paused.id)
        assert after_pause is not None and after_pause.enabled is False

    def test_an_owners_grants_are_left_as_they_are(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        mine = _owner(h)
        h.clock.t += 1
        h.loop.recover()
        assert h.loop.delegation.grant(mine.id) == mine
        assert mine.source == "owner" and mine.default_key is None
        # The judge picks the oldest grant that allows an act: the owner's.
        assert h.loop.delegation.grants()[0] == mine

    def test_a_default_whose_agent_cannot_act_waits_unseeded(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "agents": [
                    {
                        "slug": "operator",
                        "name": "Operator",
                        "description": "off",
                        "instructions": "off",
                        "enabled": False,
                    }
                ],
            }
        )
        h = Harness(tmp_path, cfg)
        h.loop.recover()
        actions = {g.action for g in h.loop.delegation.grants()}
        assert "item.retry" not in actions and "run.grant_rounds" not in actions
        assert len(actions) == 5
        assert not any(
            key.startswith("item.retry") or key.startswith("run.grant_rounds")
            for key in h.loop.delegation.seeded_default_keys()
        )

    def test_two_processes_starting_at_once_make_no_duplicates(self, tmp_path: Path) -> None:
        path = LanternHome(tmp_path).state_db
        DaemonStore(path).close()
        stores = [DelegationStore(DaemonStore(path)) for _ in range(4)]
        barrier = threading.Barrier(len(stores))
        written: list[int] = []
        errors: list[BaseException] = []

        def start(store: DelegationStore) -> None:
            barrier.wait()
            try:
                written.append(len(store.seed_defaults(DEFAULT_GRANTS, now=1.0)))
            except BaseException as exc:  # pragma: no cover - the failure is the assertion
                errors.append(exc)

        threads = [threading.Thread(target=start, args=(s,)) for s in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert sorted(written) == [0, 0, 0, len(TABLE)]
        assert len(stores[0].grants()) == len(TABLE)
        for store in stores:
            store.dstore.close()

    def test_the_index_refuses_a_second_grant_for_one_default(self, tmp_path: Path) -> None:
        dstore = DaemonStore(LanternHome(tmp_path).state_db)
        store = DelegationStore(dstore)
        store.seed_defaults(DEFAULT_GRANTS, now=1.0)
        with pytest.raises(IntegrityError), dstore.transaction() as session:
            session.add(
                GrantRow(
                    grant_id="grant_dup",
                    agent_slug="critic",
                    action="plan.run",
                    conditions_json="{}",
                    created_at=2.0,
                    updated_at=2.0,
                    revision=1,
                    source="default",
                    default_key=DEFAULT_KEYS[0],
                )
            )
        dstore.close()


class TestRestoreOperation:
    def test_the_restore_has_an_effect_and_a_judge(self, tmp_path: Path) -> None:
        assert "grant.restore_defaults" in EFFECTS
        h = Harness(tmp_path)
        op = Operation(
            id="op_1",
            action="grant.restore_defaults",
            target_kind="grant",
            target_key="defaults",
            state="running",
            effect=EFFECTS["grant.restore_defaults"],
            actor={"kind": "client", "id": "usr_owner"},
            request={},
            accepted_at=1.0,
        )
        state, code, _ = _judge(h.loop, op)
        assert (state, code) == ("failed", "interrupted_before_effect")
        h.loop.seed_default_grants()
        assert _judge(h.loop, op)[0] == "succeeded"


class TestTheDefaultsAct:
    def test_triage_retries_a_recent_ci_timeout_once(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h)
        h.loop.seed_default_grants()
        _triage(h)
        item = h.dstore.get(item_id)
        assert item is not None and item.state == "queued"
        (allowed,) = _decisions(h)
        retry = next(g for g in h.loop.delegation.grants() if g.action == "item.retry")
        assert allowed.outcome == "allow" and allowed.grant_id == retry.id
        # It runs again and fails the same way: the default allows one retry.
        h.outcomes = ["failed"]
        _reasons(h, CI_TIMEOUT)
        h.clock.t += 5
        h.loop._dispatch_pass(h.clock(), discovered=[])
        assert h.dstore.get(item_id).state == "failed"  # type: ignore[union-attr]
        _triage(h)
        _allowed, escalated = _decisions(h)
        assert escalated.outcome == "escalate"
        assert "max_retries is 1 and retries is 1" in escalated.reason
        assert h.dstore.get(item_id).state == "failed"  # type: ignore[union-attr]

    def test_triage_leaves_a_cause_outside_the_defaults_to_a_person(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h, reason="the pull request conflicts with its base branch")
        h.loop.seed_default_grants()
        _triage(h)
        assert h.dstore.get(item_id).state == "failed"  # type: ignore[union-attr]
        (escalated,) = _decisions(h)
        assert escalated.outcome == "escalate"

    def test_a_manual_plan_is_never_touched_by_the_defaults(self, tmp_path: Path) -> None:
        w = World(tmp_path)
        w.loop.seed_default_grants()
        plan = w.plan(advance="manual")
        for _ in range(3):
            w.tick()
            w.later(900)
        assert w.breakdowns() == [] and w.decisions() == []
        assert w.get(plan).revision == plan.revision

    def test_an_auto_plan_is_broken_down_under_the_default(self, tmp_path: Path) -> None:
        w = World(tmp_path)
        w.loop.seed_default_grants()
        plan = w.plan()
        w.tick()
        (item,) = w.breakdowns(plan)
        (decision,) = w.decisions(plan)
        breakdown = next(g for g in w.loop.delegation.grants() if g.action == "plan.breakdown")
        assert decision.action == "plan.breakdown" and decision.outcome == "allow"
        assert decision.grant_id == breakdown.id
        # An epic's breakdown proposes tasks: the level the default names.
        assert decision.attrs["level"] == "task"
        assert item.state == "queued"


# -- the revision -------------------------------------------------------------------


def _seed_owner_grant(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO daemon_grants (grant_id, agent_slug, action, conditions_json, "
            "daily_limit, enabled, note, created_by, created_by_display, created_at, "
            "updated_at, revision) VALUES ('grant_mine', 'critic', 'plan.approve', "
            "'{\"max_children\": 3}', 1, 1, 'mine', 'usr_owner', 'Owner', 1, 1, 2)"
        )


class TestTheRevision:
    def test_existing_grants_read_as_an_owners(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        _upgrade_to(path, "0053")
        _seed_owner_grant(path)

        _upgrade_to(path, "0054")

        columns = {str(row[1]): row for row in _query(path, "PRAGMA table_info(daemon_grants)")}
        assert columns["source"][3] == 1  # NOT NULL
        assert columns["default_key"][3] == 0
        assert _query(
            path, "SELECT grant_id, source, default_key, revision FROM daemon_grants"
        ) == [("grant_mine", "owner", None, 2)]
        indexes = {
            str(row[1]): int(row[2]) for row in _query(path, "PRAGMA index_list(daemon_grants)")
        }
        assert indexes["idx_daemon_grants_default_key"] == 1  # unique

    def test_the_upgrade_runs_again_on_a_rewound_stamp(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        _upgrade_to(path, "0054")
        _seed_owner_grant(path)
        _stamp(path, "0053")
        _upgrade_to(path, "0054")
        assert _query(path, "SELECT grant_id, source FROM daemon_grants") == [
            ("grant_mine", "owner")
        ]

    def test_an_upgrade_seeds_the_defaults_beside_the_owners_grants(self, tmp_path: Path) -> None:
        path = LanternHome(tmp_path).state_db
        path.parent.mkdir(parents=True, exist_ok=True)
        _upgrade_to(path, "0053")
        _seed_owner_grant(path)
        _head(path)
        cfg = Config.model_validate({"home": str(tmp_path), "github": {"repo": "o/r"}})
        h = Harness(tmp_path, cfg)
        assert h.config.paths.state_db == path
        h.loop.recover()
        grants = h.loop.delegation.grants()
        mine = h.loop.delegation.grant("grant_mine")
        assert mine is not None and mine.source == "owner" and mine.revision == 2
        assert _shape(_defaults(h.loop.delegation)) == TABLE
        assert all(g.enabled for g in grants)
        assert len(grants) == len(TABLE) + 1
        assert h.dstore.values_with_prefix(SEEDED_PREFIX).keys() == {
            SEEDED_PREFIX + key for key in DEFAULT_KEYS
        }
