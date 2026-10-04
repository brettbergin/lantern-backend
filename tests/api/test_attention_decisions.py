"""Decisions on the attention list (feature ``attention.decisions``).

What an agent escalated to a person is an ``escalation`` entry, decided
from the list: ``approve`` takes the step as the person through the
step's own command and resolves the ledger row ``acted``; ``decline``
resolves it ``declined``. A ``manual`` plan's waiting questions and
proposed levels are entries too (``plan_questions``, ``plan_proposal``);
a plan that advances itself surfaces only through its escalations.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from lantern.api import escalations
from lantern.api.attention_act import APPROVED_OPERATIONS
from lantern.api.attention_events import AttentionTracker
from lantern.api.commands import ACTIONS as COMMANDS
from lantern.daemon.controls.delegation import DELEGABLE_ACTIONS, Decision
from lantern.daemon.controls.delegation_store import DecisionRecord
from lantern.daemon.controls.operations import EFFECTS
from lantern.daemon.controls.principal import ALL_CAPABILITIES, Capability
from lantern.engine.planning import Clarification, PlanQuestion
from tests.api.conftest import Api
from tests.api.test_attention import _blocked, _entries
from tests.api.test_attention_act import _act, _failed_task, _recorded
from tests.api.test_plans_publish import _add, _create, _forge, _node
from tests.api.test_plans_run import _published, _task
from tests.unit.test_daemon_loop import gh_item

READ: frozenset[Capability] = frozenset({"runs:read"})
#: What a member holds of the plan and work capabilities.
MEMBER: frozenset[Capability] = frozenset({"runs:read", "plans:create"})
#: An admin: everything but policy.
ADMIN: frozenset[Capability] = ALL_CAPABILITIES - {"policy:manage", "credentials:manage"}


def _escalate(api: Api, action: str, **refs: Any) -> DecisionRecord:
    """An escalation the plan driver would write: no grant covered it."""
    attrs = dict(refs.pop("attrs", {}))
    record: DecisionRecord = api.loop.delegation.record(
        Decision(outcome="escalate", reason=f"no enabled grant lets planner take {action}"),
        agent_slug="planner",
        action=action,
        attrs=attrs,
        now=api.clock(),
        **refs,
    )
    return record


def _proposed(api: Api, plan: dict[str, Any], *titles: str) -> dict[str, Any]:
    """``titles`` added under the root, as the planner proposes them."""
    headers = api.bearer()
    for title in titles:
        plan = _add(api, headers, plan, title=title, kind="code", acceptance_criteria=["works"])
    current = api.ctx.plans.get(plan["id"])
    marked = [
        replace(node, state="proposed", proposed_by="agent:planner")
        for node in current.nodes
        if node.title in titles
    ]
    changed = api.ctx.plans.store.apply(
        current.id, expected_revision=current.revision, now=api.clock(), upsert=marked
    )
    return {"id": changed.id, "root_id": changed.root_id, "revision": changed.revision}


def _plan(api: Api, *titles: str) -> dict[str, Any]:
    """A manual epic with ``titles`` proposed under it."""
    plan = _create(api, api.bearer(), goal="Ship it")
    return _proposed(api, plan, *titles)


def _kinds(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["kind"] for entry in entries]


def _one(api: Api, kind: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    (entry,) = [e for e in _entries(api, headers) if e["kind"] == kind]
    return dict(entry)


def _ledger(api: Api, record: DecisionRecord) -> DecisionRecord:
    held = api.loop.delegation.decision(record.id)
    assert held is not None
    return held


def _settle(api: Api) -> None:
    """One attention tracker pass."""
    tracker = AttentionTracker(api.ctx)
    tracker.diff(api.clock())


# -- the map ------------------------------------------------------------------------------


def test_every_delegable_action_reads_in_plain_words_with_a_capability() -> None:
    assert set(escalations.ESCALATED) == set(DELEGABLE_ACTIONS)
    for action, escalated in escalations.ESCALATED.items():
        assert "{target}" in escalated.phrase, action
        if escalated.approvable:
            # Approving runs the step's own command, recorded as it records.
            assert APPROVED_OPERATIONS[action] in EFFECTS
        else:
            assert action not in APPROVED_OPERATIONS
    assert set(APPROVED_OPERATIONS) == {a for a, e in escalations.ESCALATED.items() if e.approvable}
    assert "decision.decline" in EFFECTS
    # The capability is the one the step's own route asks of a person.
    for action in ("item.retry", "run.grant_rounds"):
        assert escalations.capability(action) == COMMANDS[action][0]
    assert escalations.capability("plan.propose") == "policy:manage"
    # An action this build does not know is shown, never taken, owners only.
    assert escalations.actions("plan.dream") == ("decline",)
    assert escalations.capability("plan.dream") == "policy:manage"


# -- entries ------------------------------------------------------------------------------


class TestEscalationEntries:
    @pytest.mark.parametrize(
        ("action", "capability", "offered", "words"),
        [
            ("plan.approve", "plans:create", ["approve", "decline"], "approve the level proposed"),
            ("plan.publish", "plans:publish", ["approve", "decline"], "publish the level under"),
            ("plan.run", "plans:publish", ["approve", "decline"], "start running the tasks of"),
            ("plan.run.retry", "plans:publish", ["approve", "decline"], "retry the failed task"),
            ("plan.breakdown", "plans:create", ["approve", "decline"], "break"),
            ("plan.propose", "policy:manage", ["decline"], "propose a plan for"),
        ],
    )
    def test_each_plan_action_is_a_decision_naming_its_agent_and_target(
        self, api: Api, action: str, capability: str, offered: list[str], words: str
    ) -> None:
        plan = _plan(api, "A")
        # The root with a proposed level under it: still waiting for each
        # step but a breakdown, which waits on a node with nothing under it.
        node_id = plan["root_id"]
        if action == "plan.breakdown":
            node_id = _node(_read(api, plan), "A")["id"]
        record = _escalate(
            api, action, plan_id=plan["id"], node_id=node_id, attrs={"repository": "o/r"}
        )
        entry = _one(api, "escalation")
        assert entry["id"] == f"escalation:{record.id}"
        assert entry["group"] == "decision" and entry["state"] == "escalated"
        assert entry["title"].startswith("planner asks to ") and words in entry["title"]
        assert entry["reason"] == record.reason
        assert entry["agent"] == "planner" and entry["decision_id"] == record.id
        assert entry["decision_action"] == action
        assert entry["plan_id"] == plan["id"] and entry["node_id"] == node_id
        assert entry["repository"] == "o/r" and entry["repository_id"]
        assert entry["revision"] == api.ctx.plans.get(plan["id"]).revision
        assert [a["action"] for a in entry["actions"]] == offered
        assert {a["capability"] for a in entry["actions"]} == {capability}

    def test_an_item_escalation_names_the_item_and_its_run(self, api: Api) -> None:
        run_id = _blocked(api)
        (item,) = [i for i in api.loop.dstore.items() if i.source_key == "1"]
        record = _escalate(api, "item.retry", item_id=item.item_id, run_id=run_id, repository="o/r")
        entry = _one(api, "escalation")
        assert entry["title"] == "planner asks to retry “Do 1”"
        assert entry["item_id"] and entry["run_id"]
        assert entry["revision"] == api.loop.dstore.get(item.item_id).revision
        assert [(a["action"], a["capability"]) for a in entry["actions"]] == [
            ("approve", "runs:control"),
            ("decline", "runs:control"),
        ]
        # The failed item stays its own entry: the agent asking is news of its own.
        assert sorted(_kinds(_entries(api))) == ["escalation", "item"]
        assert record.unresolved

    def test_a_member_sees_what_it_may_take(self, api: Api) -> None:
        plan = _plan(api, "A")
        _escalate(api, "plan.publish", plan_id=plan["id"], node_id=plan["root_id"])
        _escalate(api, "plan.approve", plan_id=plan["id"], node_id=plan["root_id"])
        allowed = {
            e["decision_action"]: {a["allowed"] for a in e["actions"]}
            for e in _entries(api, api.bearer(MEMBER))
            if e["kind"] == "escalation"
        }
        assert allowed == {"plan.publish": {False}, "plan.approve": {True}}


def _read(api: Api, plan: dict[str, Any]) -> dict[str, Any]:
    return dict(api.client.get(f"/v1/plans/{plan['id']}", headers=api.bearer()).json())


class TestPlanEntries:
    def test_a_proposed_level_is_one_entry_approved_from_the_list(self, api: Api) -> None:
        plan = _plan(api, "A", "B")
        entry = _one(api, "plan_proposal")
        assert entry["id"] == f"plan_proposal:{plan['id']}:{plan['root_id']}"
        assert entry["group"] == "decision" and entry["state"] == "proposed"
        assert entry["title"] == "2 proposed tasks under “An epic” wait for approval"
        assert entry["reason"] == "proposed by agent:planner"
        assert entry["revision"] == plan["revision"]
        assert [(a["action"], a["capability"], a["allowed"]) for a in entry["actions"]] == [
            ("approve", "plans:create", True)
        ]
        # Without plans:create the approval is not offered at all.
        reader = _one(api, "plan_proposal", api.bearer(READ))
        assert reader["actions"] == []
        refused = _act(api, api.bearer(READ), entry["id"], "approve", key="p0")
        assert refused.status_code == 403 and refused.json()["capability"] == "plans:create"

        headers = api.bearer(MEMBER)
        stale = _act(
            api, headers, entry["id"], "approve", key="p1", expected_revision=plan["revision"] + 1
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        approved = _act(
            api, headers, entry["id"], "approve", key="p2", expected_revision=plan["revision"]
        )
        assert approved.status_code == 200, approved.text
        body = approved.json()
        states = {n["title"]: n["state"] for n in body["result"]["plan"]["nodes"]}
        assert states["A"] == states["B"] == "approved"
        assert body["result"]["operation_id"] == body["operation_id"]
        assert body["still_waiting"] is False and body["decision"] is None
        (op,) = _recorded(api, "plan.approve")
        assert op["id"] == body["operation_id"] and op["state"] == "succeeded"
        replay = _act(
            api, headers, entry["id"], "approve", key="p2", expected_revision=plan["revision"]
        )
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["operation_id"] == body["operation_id"]
        assert len(_recorded(api, "plan.approve")) == 1
        assert "plan_proposal" not in _kinds(_entries(api))

    def test_a_plan_that_advances_itself_shows_no_proposal(self, api: Api) -> None:
        plan = _plan(api, "A")
        assert "plan_proposal" in _kinds(_entries(api))
        current = api.ctx.plans.get(plan["id"])
        api.ctx.plans.store.apply(
            current.id, expected_revision=current.revision, now=api.clock(), advance="auto"
        )
        assert _entries(api) == []
        # What it needs from a person reaches them as an escalation.
        _escalate(api, "plan.approve", plan_id=plan["id"], node_id=plan["root_id"])
        assert _kinds(_entries(api)) == ["escalation"]

    def _ask(self, api: Api, plan: dict[str, Any]) -> None:
        current = api.ctx.plans.get(plan["id"])
        asking = replace(
            current.root,
            generation=Clarification(
                run_id="rq1",
                questions=[
                    PlanQuestion.model_validate(
                        {"id": "q1", "prompt": "Which store?", "choices": ["pg", "sqlite"]}
                    )
                ],
                asked_at=api.clock(),
            ),
        )
        api.ctx.plans.store.apply(
            current.id, expected_revision=current.revision, now=api.clock(), upsert=[asking]
        )

    def test_questions_with_no_item_are_a_plan_entry(self, api: Api) -> None:
        plan = _create(api, api.bearer())
        self._ask(api, plan)
        entry = _one(api, "plan_questions")
        assert entry["id"] == f"plan_questions:{plan['id']}:{plan['root_id']}:rq1"
        assert entry["state"] == "awaiting_answers" and entry["group"] == "decision"
        assert entry["title"] == "Questions about “An epic” wait for answers"
        assert entry["reason"] == "1 question to answer on the plan"
        assert entry["plan_id"] == plan["id"] and entry["node_id"] == plan["root_id"]
        assert entry["actions"] == []

    def test_questions_and_the_item_waiting_on_them_are_one_entry(self, api: Api) -> None:
        plan = _create(api, api.bearer())
        self._ask(api, plan)
        dstore = api.harness.dstore
        dstore.upsert_new(
            gh_item("7", kind="plan", plan_id=plan["id"], plan_node_id=plan["root_id"]),
            api.clock(),
        )
        dstore.mark_awaiting_answers("gh:7", api.clock())
        (entry,) = _entries(api)
        # The item's entry keeps the id clients already key on.
        assert entry["kind"] == "item" and entry["id"].startswith("item:")
        assert entry["id"].endswith(":awaiting_answers")
        assert (entry["plan_id"], entry["node_id"]) == (plan["id"], plan["root_id"])
        # Dismissing the item puts the questions away too.
        dismissed = _act(api, api.bearer(), entry["id"], "dismiss", key="d1")
        assert dismissed.status_code == 200, dismissed.text
        assert _entries(api) == []


# -- acting on an escalation --------------------------------------------------------------


class TestDecline:
    def test_decline_resolves_the_decision_and_nothing_else(self, api: Api) -> None:
        plan = _plan(api, "A")
        record = _escalate(api, "plan.approve", plan_id=plan["id"], node_id=plan["root_id"])
        entry = next(e for e in _entries(api) if e["kind"] == "escalation")
        member = api.bearer(MEMBER)
        declined = _act(api, member, entry["id"], "decline", key="n1")
        assert declined.status_code == 200, declined.text
        body = declined.json()
        assert body["decision"]["resolution"] == "declined"
        assert body["result"]["decision"]["id"] == record.id
        held = _ledger(api, record)
        assert held.resolution == "declined" and held.resolved_by and held.resolved_at
        (op,) = _recorded(api, "decision.decline")
        assert op["id"] == body["operation_id"] and op["state"] == "succeeded"
        assert body["still_waiting"] is False
        # The level is exactly as it was; the proposal still waits.
        assert _kinds(_entries(api)) == ["plan_proposal"]
        assert api.ctx.plans.get(plan["id"]).revision == plan["revision"]
        # A replay answers the first decline; another action under the key conflicts.
        replay = _act(api, member, entry["id"], "decline", key="n1")
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["operation_id"] == body["operation_id"]
        assert replay.json()["decision"]["resolution"] == "declined"
        conflict = _act(api, member, entry["id"], "approve", key="n1")
        assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
        assert len(_recorded(api, "decision.decline")) == 1
        gone = _act(api, member, entry["id"], "decline", key="n2")
        assert gone.status_code == 409 and gone.json()["code"] == "not_waiting"

    def test_a_propose_escalation_is_the_owners_and_only_declined(self, api: Api) -> None:
        record = _escalate(api, "plan.propose", attrs={"repository": "o/r"})
        entry = _one(api, "escalation")
        assert entry["title"] == "planner asks to propose a plan for o/r"
        approve = _act(api, api.bearer(), entry["id"], "approve", key="a1")
        assert approve.status_code == 409 and approve.json()["code"] == "not_eligible"
        for caps in (MEMBER, ADMIN):
            refused = _act(api, api.bearer(caps), entry["id"], "decline", key="a2")
            assert refused.status_code == 403
            assert refused.json()["capability"] == "policy:manage"
        declined = _act(api, api.bearer(), entry["id"], "decline", key="a3")
        assert declined.status_code == 200, declined.text
        assert _ledger(api, record).resolution == "declined"

    def test_a_member_cannot_decide_what_only_an_admin_could_do(self, api: Api) -> None:
        plan = _plan(api, "A")
        _escalate(api, "plan.publish", plan_id=plan["id"], node_id=plan["root_id"])
        entry = _one(api, "escalation")
        for action in ("approve", "decline"):
            refused = _act(api, api.bearer(MEMBER), entry["id"], action, key=action)
            assert refused.status_code == 403
            assert refused.json()["capability"] == "plans:publish"


class TestApprove:
    def test_approving_an_escalated_approval_approves_the_level(self, api: Api) -> None:
        plan = _plan(api, "A", "B")
        record = _escalate(api, "plan.approve", plan_id=plan["id"], node_id=plan["root_id"])
        entry = _one(api, "escalation")
        approved = _act(api, api.bearer(MEMBER), entry["id"], "approve", key="a1")
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert {n["state"] for n in body["result"]["plan"]["nodes"] if n["title"] in "AB"} == {
            "approved"
        }
        held = _ledger(api, record)
        assert held.resolution == "acted" and held.resolved_by
        assert body["decision"]["resolution"] == "acted"
        (op,) = _recorded(api, "plan.approve")
        assert op["id"] == body["operation_id"]
        assert op["actor"]["kind"] != "agent"
        assert _entries(api) == []

    def test_approving_an_escalated_publish_writes_the_level_to_the_forge(self, api: Api) -> None:
        fake = _forge(api)
        plan = _plan(api, "A")
        current = api.ctx.plans.get(plan["id"])
        approved = [replace(n, state="approved") for n in current.children(current.root_id)]
        current = api.ctx.plans.store.apply(
            current.id, expected_revision=current.revision, now=api.clock(), upsert=approved
        )
        record = _escalate(api, "plan.publish", plan_id=plan["id"], node_id=plan["root_id"])
        entry = _one(api, "escalation")
        admin = api.bearer(ADMIN)
        published = _act(api, admin, entry["id"], "approve", key="p1")
        assert published.status_code == 200, published.text
        assert {r["outcome"] for r in published.json()["result"]["results"]} == {"created"}
        assert len(fake.issues_created) == 2  # the epic, then its task
        assert _ledger(api, record).resolution == "acted"
        assert len(_recorded(api, "plan.publish")) == 1
        replay = _act(api, admin, entry["id"], "approve", key="p1")
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert len(fake.issues_created) == 2 and len(_recorded(api, "plan.publish")) == 1

    def test_approving_an_escalated_run_starts_the_epic(self, api: Api) -> None:
        _fake, _headers, plan = _published(api)
        record = _escalate(api, "plan.run", plan_id=plan["id"], node_id=plan["root_id"])
        entry = _one(api, "escalation")
        started = _act(api, api.bearer(ADMIN), entry["id"], "approve", key="r1")
        assert started.status_code == 201, started.text
        body = started.json()
        assert body["result"]["state"] == "running"
        assert _task(body["result"], "A")["state"] == "queued"
        assert _ledger(api, record).resolution == "acted"

    def test_approving_an_escalated_task_retry_requeues_it(self, api: Api) -> None:
        plan, run, task_entry = _failed_task(api)
        record = _escalate(
            api,
            "plan.run.retry",
            plan_id=plan["id"],
            node_id=task_entry["node_id"],
            epic_run_id=run["id"],
        )
        entry = _one(api, "escalation")
        assert entry["title"] == "planner asks to retry the failed task “A”"
        retried = _act(api, api.bearer(ADMIN), entry["id"], "approve", key="t1")
        assert retried.status_code == 200, retried.text
        assert _task(retried.json()["result"], "A")["state"] in ("queued", "running")
        assert _ledger(api, record).resolution == "acted"

    def test_approving_an_escalated_item_retry_requeues_the_item(self, api: Api) -> None:
        run_id = _blocked(api)
        (item,) = [i for i in api.loop.dstore.items() if i.source_key == "1"]
        record = _escalate(api, "item.retry", item_id=item.item_id, run_id=run_id, repository="o/r")
        entry = _one(api, "escalation")
        refused = _act(api, api.bearer(MEMBER), entry["id"], "approve", key="i0")
        assert refused.status_code == 403 and refused.json()["capability"] == "runs:control"
        retried = _act(api, api.bearer(ADMIN), entry["id"], "approve", key="i1")
        assert retried.status_code == 200, retried.text
        assert api.loop.dstore.get(item.item_id).state == "queued"
        assert _ledger(api, record).resolution == "acted"
        assert len(_recorded(api, "item.retry")) == 1

    def test_approving_an_escalated_breakdown_queues_it(self, api: Api) -> None:
        from tests.api.test_plans import _create as _person_plan

        plan = _person_plan(api, api.bearer())
        record = _escalate(api, "plan.breakdown", plan_id=plan["id"], node_id=plan["root_id"])
        entry = _one(api, "escalation")
        accepted = _act(api, api.bearer(MEMBER), entry["id"], "approve", key="b1")
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["result"]["item"]["kind"] == "plan"
        assert accepted.json()["result"]["operation"]["action"] == "item.admit"
        assert _ledger(api, record).resolution == "acted"

    def test_a_refused_step_leaves_the_escalation_waiting(self, api: Api) -> None:
        plan = _plan(api, "A")
        record = _escalate(api, "plan.publish", plan_id=plan["id"], node_id=plan["root_id"])
        approved = [
            replace(n, state="approved")
            for n in api.ctx.plans.get(plan["id"]).children(plan["root_id"])
        ]
        current = api.ctx.plans.get(plan["id"])
        api.ctx.plans.store.apply(
            current.id, expected_revision=current.revision, now=api.clock(), upsert=approved
        )
        entry = _one(api, "escalation")
        # No forge connection: the publish is refused as its route refuses it.
        api.loop.github = None
        refused = _act(api, api.bearer(ADMIN), entry["id"], "approve", key="x1")
        assert refused.status_code == 503, refused.text
        assert _ledger(api, record).unresolved


# -- resolution hygiene -------------------------------------------------------------------


class TestSuperseded:
    def test_a_step_someone_took_elsewhere_leaves_the_list_and_the_tracker_resolves_it(
        self, api: Api
    ) -> None:
        plan = _plan(api, "A")
        record = _escalate(api, "plan.approve", plan_id=plan["id"], node_id=plan["root_id"])
        assert "escalation" in _kinds(_entries(api))
        approved = api.client.post(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/approve",
            json={"expected_revision": plan["revision"]},
            headers=api.bearer(),
        )
        assert approved.status_code == 200, approved.text
        # Off the list as soon as it is read; the read wrote nothing.
        assert _entries(api) == []
        assert _ledger(api, record).unresolved
        _settle(api)
        held = _ledger(api, record)
        assert held.resolution == "superseded" and held.resolved_by is None

    def test_an_escalation_about_something_gone_is_superseded(self, api: Api) -> None:
        record = _escalate(api, "plan.publish", plan_id="plan_gone", node_id="node_gone")
        item_record = _escalate(api, "item.retry", item_id="gh:404", repository="o/r")
        assert _entries(api) == []
        _settle(api)
        assert _ledger(api, record).resolution == "superseded"
        assert _ledger(api, item_record).resolution == "superseded"

    def test_an_escalation_still_needed_is_left_alone(self, api: Api) -> None:
        plan = _plan(api, "A")
        record = _escalate(api, "plan.approve", plan_id=plan["id"], node_id=plan["root_id"])
        _settle(api)
        assert _ledger(api, record).unresolved


def test_the_feature_is_advertised(api: Api) -> None:
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "attention.decisions" in features
