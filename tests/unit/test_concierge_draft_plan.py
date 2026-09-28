"""The concierge drafts a plan from chat, on the asker's yes (#2351).

When an ask is too big for one run, the concierge offers a plan instead of
admitting one oversized run. On the person's explicit yes, ``draft_plan``
creates a DRAFT plan pre-filled from the conversation through the plan
service, as the person who asked (``plans:create``, exactly as ``POST
/v1/plans`` requires), and answers with the link Angie and Lantern open:
``/plans/<plan_id>``. It never publishes, approves, breaks a node down or
starts an epic run: nothing reaches the forge and nothing is queued.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from sbxloop.daemon.controls.principal import ROLE_CAPABILITIES, Principal
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ApiEventRow
from sbxloop.plans.model import Plan
from sbxloop.plans.store import PlanStore
from tests.unit.test_daemon_concierge import FakeGithub, make

#: What the concierge pre-fills from the conversation.
DRAFT: dict[str, Any] = {
    "level": "initiative",
    "title": "Offline mode for the field app",
    "goal": "Field staff can record visits with no signal and sync them later.",
    "acceptance_criteria": [
        "A visit recorded offline appears on the server after the next sync",
        "A conflicting edit is shown to the person, never silently dropped",
    ],
    "constraints": "No new paid services.",
    "non_goals": "Offline editing of the admin console.",
    "context": "Asked for in chat: visits are lost whenever the signal drops.",
    "confirmation": "yes, draft it",
}


def _person(role: str, user_id: str, name: str) -> Principal:
    return Principal(
        kind="client",
        id=user_id,
        display=name,
        via="collaboration",
        capabilities=ROLE_CAPABILITIES[role],  # type: ignore[index]
    )


READER = Principal(
    kind="client",
    id="u-reader",
    display="Reader",
    via="collaboration",
    capabilities=frozenset({"runs:read", "items:create"}),
)


def _turn(
    tmp_path: Path,
    args: dict[str, Any],
    principal: Principal | None,
    *,
    config: dict[str, Any] | None = None,
    calls: int = 1,
) -> tuple[list[str], DaemonStore, FakeGithub]:
    github = FakeGithub()
    concierge, client, _, _, dstore = make(
        tmp_path,
        [{"calls": [("draft_plan", args)] * calls, "text": "done"}],
        github=github,
        config=config,
    )
    try:
        concierge.submit_turn(
            "this is a big one",
            author="Guest",
            author_id="u-guest",
            message_id="m-1",
            principal=principal,
        ).result(timeout=10)
    finally:
        concierge.close()
    return [r.text or "" for r in client.responses], dstore, github


def _plans(dstore: DaemonStore) -> list[Plan]:
    return PlanStore(dstore).all()


def _events(dstore: DaemonStore, type_: str) -> list[ApiEventRow]:
    with dstore.read() as session:
        return list(session.scalars(select(ApiEventRow).where(ApiEventRow.type == type_)))


def _nothing_else_happened(dstore: DaemonStore, github: FakeGithub) -> None:
    """No run admitted (no breakdown, no epic run) and no forge write."""
    assert dstore.items() == []
    assert github.calls == []
    assert getattr(github, "created", []) == []


class TestTheAskerMustHoldPlansCreate:
    @pytest.mark.parametrize("principal", [None, READER], ids=["no-principal", "reader"])
    def test_a_turn_without_plans_create_drafts_nothing(
        self, tmp_path: Path, principal: Principal | None
    ) -> None:
        (text,), dstore, github = _turn(tmp_path, DRAFT, principal)
        who = "unvouched" if principal is None else "u-reader"
        assert f"{who} (via concierge) lacks plans:create" in text, text
        assert "Nothing was drafted." in text
        assert _plans(dstore) == []
        assert _events(dstore, "plan.created") == []
        _nothing_else_happened(dstore, github)


class TestADraftOnTheAskersYes:
    def test_the_draft_is_the_askers_with_the_conversations_sections(self, tmp_path: Path) -> None:
        (text,), dstore, github = _turn(tmp_path, DRAFT, _person("member", "u-guest", "Guest"))
        (plan,) = _plans(dstore)
        assert plan.state == "draft"
        assert (plan.created_by, plan.created_by_display) == ("u-guest", "Guest")
        root = plan.root
        assert (root.level, root.repository, root.state, root.origin) == (
            "initiative",
            "owner/repo",
            "draft",
            "person",
        )
        assert root.title == "Unplanned initiative" and root.goal == ""
        assert root.acceptance_criteria == ()
        for key in ("title", "goal", "acceptance_criteria", "constraints", "non_goals", "context"):
            assert plan.input[key] == DRAFT[key]
        # A draft alone: no children proposed, nothing approved or published.
        assert plan.nodes == (root,)
        # The link Angie and Lantern open, and the plan's own words.
        assert f"[{DRAFT['title']}](/plans/{plan.id})" in text, text
        assert plan.id in text and "draft" in text
        _nothing_else_happened(dstore, github)

    def test_the_service_records_plan_created_as_the_asker_via_the_concierge(
        self, tmp_path: Path
    ) -> None:
        _, dstore, _ = _turn(tmp_path, DRAFT, _person("member", "u-guest", "Guest"))
        (plan,) = _plans(dstore)
        (event,) = _events(dstore, "plan.created")
        assert json.loads(event.data_json) == {
            "plan_id": plan.id,
            "level": "initiative",
            "repository": "owner/repo",
        }
        actor = json.loads(event.actor_json or "{}")
        assert (actor["id"], actor["display"], actor["via"]) == ("u-guest", "Guest", "concierge")

    def test_a_lone_epic_can_be_drafted(self, tmp_path: Path) -> None:
        (text,), dstore, _ = _turn(
            tmp_path,
            {"level": "epic", "title": "Export reports", "confirmation": "yes"},
            _person("admin", "u-ada", "Ada"),
        )
        (plan,) = _plans(dstore)
        assert plan.root.level == "epic" and plan.input["title"] == "Export reports"
        assert f"(/plans/{plan.id})" in text

    def test_a_retried_call_links_the_same_draft(self, tmp_path: Path) -> None:
        """A replayed turn (the session retried) must not leave two drafts."""
        texts, dstore, _ = _turn(tmp_path, DRAFT, _person("member", "u-guest", "Guest"), calls=2)
        (plan,) = _plans(dstore)
        assert f"(/plans/{plan.id})" in texts[0] and f"(/plans/{plan.id})" in texts[1]
        assert "already" in texts[1]
        assert len(_events(dstore, "plan.created")) == 1


class TestNothingIsDraftedWithoutWhatItNeeds:
    def test_no_confirmation_drafts_nothing(self, tmp_path: Path) -> None:
        args = {k: v for k, v in DRAFT.items() if k != "confirmation"}
        (text,), dstore, github = _turn(tmp_path, args, _person("member", "u-guest", "Guest"))
        assert "own words" in text and "confirmation" in text, text
        assert _plans(dstore) == []
        _nothing_else_happened(dstore, github)

    def test_a_task_is_not_a_plan(self, tmp_path: Path) -> None:
        (text,), dstore, _ = _turn(
            tmp_path, {**DRAFT, "level": "task"}, _person("member", "u-guest", "Guest")
        )
        assert "initiative or an epic" in text, text
        assert _plans(dstore) == []

    def test_an_unknown_repository_is_named(self, tmp_path: Path) -> None:
        (text,), dstore, _ = _turn(
            tmp_path, {**DRAFT, "repo": "someone/else"}, _person("member", "u-guest", "Guest")
        )
        assert "someone/else" in text and "Nothing was drafted." in text, text
        assert _plans(dstore) == []

    def test_several_repositories_need_one_named(self, tmp_path: Path) -> None:
        (text,), dstore, _ = _turn(
            tmp_path,
            DRAFT,
            _person("member", "u-guest", "Guest"),
            config={"github": {"repos": [{"repo": "owner/repo"}, {"repo": "owner/other"}]}},
        )
        assert "explicit `repo`" in text and "Nothing was drafted." in text, text
        assert _plans(dstore) == []

    def test_a_repository_planning_is_off_for_is_refused_by_name(self, tmp_path: Path) -> None:
        (text,), dstore, _ = _turn(
            tmp_path,
            {**DRAFT, "repo": "owner/repo"},
            _person("member", "u-guest", "Guest"),
            config={
                "github": {
                    "repos": [
                        {"repo": "owner/repo", "planning": {"enabled": False}},
                        {"repo": "owner/other"},
                    ]
                }
            },
        )
        assert "planning is off for this repository" in text, text
        assert "Nothing was drafted." in text
        assert _plans(dstore) == []

    def test_an_overlong_section_is_refused_not_clipped(self, tmp_path: Path) -> None:
        (text,), dstore, _ = _turn(
            tmp_path, {**DRAFT, "title": "x" * 300}, _person("member", "u-guest", "Guest")
        )
        assert "title" in text and "256" in text, text
        assert _plans(dstore) == []


class TestTheToolIsOfferedWherePlanningIs:
    def test_offered_where_a_repository_can_hold_a_plan(self, tmp_path: Path) -> None:
        concierge, *_ = make(tmp_path, [], github=FakeGithub())
        try:
            assert "draft_plan" in concierge.tool_names
        finally:
            concierge.close()

    def test_removed_when_planning_is_off(self, tmp_path: Path) -> None:
        concierge, *_ = make(
            tmp_path, [], github=FakeGithub(), config={"planning": {"enabled": False}}
        )
        try:
            assert "draft_plan" not in concierge.tool_names
        finally:
            concierge.close()

    def test_a_read_only_turn_is_not_offered_it(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(tmp_path, [{"text": "ok"}], github=FakeGithub())
        try:
            concierge.submit_turn(
                "look",
                author="Guest",
                principal=_person("member", "u-guest", "Guest"),
                read_only=True,
            ).result(timeout=10)
        finally:
            concierge.close()
        (job,) = client.jobs
        assert "draft_plan" not in {spec.name for spec in job.host_tools}


def test_the_sections_are_bounded_as_the_api_bounds_them() -> None:
    """Chat drafts nothing the form would refuse: the tool's bounds are the
    API's, read from the request model so the two cannot drift."""
    from annotated_types import MaxLen

    from sbxloop.api.plan_schemas import PlanSections
    from sbxloop.daemon.concierge import _PLAN_CRITERIA_MAX, _PLAN_TEXT_LIMITS

    def bound(name: str) -> int:
        (limit,) = [
            m.max_length for m in PlanSections.model_fields[name].metadata if isinstance(m, MaxLen)
        ]
        return limit

    assert {name: bound(name) for name in _PLAN_TEXT_LIMITS} == _PLAN_TEXT_LIMITS
    assert bound("acceptance_criteria") == _PLAN_CRITERIA_MAX
