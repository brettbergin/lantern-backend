"""What becomes a push, for whom, and what happens when the relay balks.

The dispatcher reads the public chronology live — from where it stood when
it started, never from history — and turns the events a person is waiting
on into a stored notification plus a content-free ping for each of their
devices that wants it: a mention by somebody else, work they asked for
finishing or failing, a decision only they can make. Their own messages
never ping them. The ping goes to :class:`~tests.fakes.fake_relay.FakeRelay`
over HTTP; retries are driven by moving the test clock.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lantern.api.attention_events import SWEEP_S
from lantern.api.chronology import DAEMON_ACTOR
from lantern.api.collaboration import LocalUser, _event
from lantern.api.publicids import run_public_id
from lantern.db.api_models import ApiEventRow, OperationRow
from lantern.db.daemon_models import WorkItemRow
from lantern.plans.epicrun import EpicRun, EpicRunStore
from lantern.plans.model import Plan, PlanNode
from lantern.plans.store import PlanStore
from tests.api.conftest import Api, build
from tests.api.test_attention import _blocked
from tests.api.test_attention_events import _step
from tests.api.test_push_devices import (
    RELAY,
    TOKEN_A,
    TOKEN_B,
    device,
    push_api,
    register_member,
    register_owner,
)
from tests.fakes.fake_relay import FakeRelay
from tests.unit.test_daemon_loop import gh_item


@dataclass
class Room:
    api: Api
    relay: FakeRelay
    owner: LocalUser
    bob: LocalUser
    owner_headers: dict[str, str]
    bob_headers: dict[str, str]
    owner_device: str
    bob_device: str
    channel_id: str

    def step(self) -> None:
        self.api.ctx.push.dispatcher.step()

    def pushes_to(self, token: str) -> list[dict[str, Any]]:
        return [sent.payload for sent in self.relay.sent if sent.token == token]

    def notification(self, ref: str, who: str = "owner") -> dict[str, Any]:
        headers = self.owner_headers if who == "owner" else self.bob_headers
        response = self.api.client.get(f"/v1/users/me/notifications/{ref}", headers=headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def prefs(self, who: str, **prefs: Any) -> None:
        headers = self.owner_headers if who == "owner" else self.bob_headers
        token = TOKEN_A if who == "owner" else TOKEN_B
        response = self.api.client.post(
            "/v1/users/me/devices", json=device(token, prefs=prefs), headers=headers
        )
        assert response.status_code == 200, response.text

    def say(self, user: LocalUser, content: str, targets: tuple[str, ...] = ()) -> Any:
        turn, _message, _ = self.api.ctx.collaboration.accept_turn(
            user.id,
            self.channel_id,
            content=content,
            targets=targets,
            client_turn_id=None,
            client_message_id=None,
            actor=None,
            now=self.api.clock(),
        )
        return turn

    def attention(self, kind: str, *, historical: bool = False, run_id: str | None = None) -> None:
        with self.api.harness.dstore.transaction() as session:
            _event(
                session,
                "collaboration.external_work.attention",
                self.api.clock(),
                actor=DAEMON_ACTOR,
                data={
                    "channel_id": self.channel_id,
                    "work_id": "wrk_1",
                    "run_id": run_id,
                    "attention_id": f"wrk_1:{kind}",
                    "kind": kind,
                    "title": "Nightly report",
                    "body": "Work is gated.",
                    "historical": historical,
                },
            )


@pytest.fixture
def relay() -> FakeRelay:
    return FakeRelay()


@pytest.fixture
def room(tmp_path: Path, relay: FakeRelay) -> Iterator[Room]:
    api = push_api(tmp_path, relay)
    with api.client:
        owner_headers = register_owner(api)
        bob_headers = register_member(api)
        store = api.ctx.collaboration
        owner = store.user_by_username("owner")
        bob = store.user_by_username("bob")
        assert owner is not None and bob is not None
        channel = store.create_channel(owner.id, "Plans", api.clock())
        store.add_channel_member(owner.id, channel.id, bob.id, "member", api.clock())
        owner_device = api.client.post(
            "/v1/users/me/devices", json=device(TOKEN_A), headers=owner_headers
        ).json()["id"]
        bob_device = api.client.post(
            "/v1/users/me/devices", json=device(TOKEN_B), headers=bob_headers
        ).json()["id"]
        api.ctx.push.dispatcher.prime()
        yield Room(
            api,
            relay,
            owner,
            bob,
            owner_headers,
            bob_headers,
            owner_device,
            bob_device,
            channel.id,
        )
    api.ctx.close()


def agent_name(room: Room, slug: str) -> str:
    agent = room.api.ctx.agents.get(slug)
    assert agent is not None
    return agent.spec.name


# -- mentions ----------------------------------------------------------------------------


def test_a_mention_by_somebody_else_pings_the_person_named(room: Room) -> None:
    room.say(room.bob, "  hey   @OWNER,\n\tcan you look at this?  ")
    room.step()

    [ping] = room.pushes_to(TOKEN_A)
    assert ping["k"] == "mention"
    assert ping["srv"] == "home.server-1"
    assert ping["thread"] == room.channel_id
    notice = room.notification(ping["ref"])
    assert notice["kind"] == "mention"
    assert notice["channel_id"] == room.channel_id
    assert notice["title"] == "Bob Builder mentioned you"
    assert notice["body"] == "hey @OWNER, can you look at this?"
    # The author is never pinged about their own message.
    assert room.pushes_to(TOKEN_B) == []


@pytest.mark.parametrize(
    "content",
    [
        "@ownership is shared",
        "mail owner@example.test",
        "me@owner.test",
        "@owner-team please",
        "no mention at all",
    ],
)
def test_only_a_word_bounded_handle_is_a_mention(room: Room, content: str) -> None:
    room.say(room.bob, content)
    room.step()
    assert room.pushes_to(TOKEN_A) == []


def test_a_long_mention_is_cut_to_a_notification_body(room: Room) -> None:
    room.say(room.bob, "@owner " + "x" * 300)
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    body = room.notification(ping["ref"])["body"]
    assert len(body) == 140 and body.endswith("…")
    assert body == ("@owner " + "x" * 300)[:139] + "…"


def test_mentioning_yourself_is_not_news(room: Room) -> None:
    room.say(room.owner, "note to @owner: buy milk")
    room.step()
    assert room.relay.sent == []


def test_a_mention_in_a_channel_you_cannot_see_is_not_sent(room: Room) -> None:
    store = room.api.ctx.collaboration
    secret = store.create_channel(room.bob.id, "Bob's own", room.api.clock())
    store.accept_turn(
        room.bob.id,
        secret.id,
        content="talking about @owner behind their back",
        targets=(),
        client_turn_id=None,
        client_message_id=None,
        actor=None,
        now=room.api.clock(),
    )
    room.step()
    assert room.relay.sent == []


# -- work, replies and failures ------------------------------------------------------------


def _deliver(room: Room, turn_id: str, state: str, title: str | None, slug: str | None) -> None:
    room.api.ctx.collaboration.append_work_result(
        f"msg_work_{state}",
        channel_id=room.channel_id,
        turn_id=turn_id,
        content="the result",
        agent_slug=slug,
        work={"item_id": "itm_1", "state": state, "title": title, "agent_slug": slug},
        now=room.api.clock(),
    )


@pytest.mark.parametrize(
    ("state", "kind", "title", "body"),
    [
        (
            "merged",
            "work",
            "{agent} delivered Fix the login",
            "Changes merged. The result is in the chat.",
        ),
        ("completed", "work", "{agent} delivered Fix the login", "The result is in the chat."),
        (
            "failed",
            "failure",
            "{agent} could not finish Fix the login",
            "The run ended failed. The details are in the chat.",
        ),
        (
            "blocked",
            "failure",
            "{agent} could not finish Fix the login",
            "The run ended blocked. The details are in the chat.",
        ),
        (
            "cancelled",
            "failure",
            "{agent} could not finish Fix the login",
            "The run ended cancelled. The details are in the chat.",
        ),
    ],
)
def test_delivered_work_pings_the_person_who_asked(
    room: Room, state: str, kind: str, title: str, body: str
) -> None:
    turn = room.say(room.owner, "please fix the login", ("planner",))
    room.step()
    _deliver(room, turn.id, state, "Fix the login", "planner")
    room.step()

    [ping] = room.pushes_to(TOKEN_A)
    assert ping["k"] == kind
    notice = room.notification(ping["ref"])
    assert notice["title"] == title.format(agent=agent_name(room, "planner"))
    assert notice["body"] == body
    assert notice["turn_id"] == turn.id
    # Bob can see the channel, but he did not ask for this.
    assert room.pushes_to(TOKEN_B) == []


def test_work_with_no_title_is_still_named(room: Room) -> None:
    turn = room.say(room.owner, "do the thing")
    _deliver(room, turn.id, "completed", None, None)
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    assert room.notification(ping["ref"])["title"].endswith(" delivered your work")


def test_a_finished_turn_pings_its_asker_and_a_failed_one_says_why(room: Room) -> None:
    store = room.api.ctx.collaboration
    replied = room.say(room.bob, "question", ("planner",))
    store.start_turn(replied.id, room.api.clock())
    store.finish_turn(replied.id, error=None, now=room.api.clock())
    broke = room.say(room.bob, "another", ("planner",))
    store.start_turn(broke.id, room.api.clock())
    store.finish_turn(broke.id, error="the model is unavailable", now=room.api.clock())
    room.step()

    planner = agent_name(room, "planner")
    pings = room.pushes_to(TOKEN_B)
    assert [p["k"] for p in pings] == ["work", "failure"]
    first, second = (room.notification(p["ref"], "bob") for p in pings)
    assert (first["title"], first["body"]) == (
        f"{planner} replied",
        "A new reply is waiting in the chat.",
    )
    assert (second["title"], second["body"]) == (
        f"{planner} could not reply",
        "the model is unavailable",
    )
    assert room.pushes_to(TOKEN_A) == []


# -- attention and gates ----------------------------------------------------------------


def test_a_decision_goes_only_to_people_who_can_make_it(room: Room) -> None:
    room.attention("action_required")
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    assert ping["k"] == "gate"
    notice = room.notification(ping["ref"])
    assert (notice["title"], notice["body"]) == ("Nightly report", "Work is gated.")
    # Bob is a member: he cannot approve a gate.
    assert room.pushes_to(TOKEN_B) == []


@pytest.mark.parametrize(("kind", "push_kind"), [("work", "work"), ("failure", "failure")])
def test_finished_attention_goes_to_the_channels_members(
    room: Room, kind: str, push_kind: str
) -> None:
    room.attention(kind)
    room.step()
    assert [p["k"] for p in room.pushes_to(TOKEN_A)] == [push_kind]
    assert [p["k"] for p in room.pushes_to(TOKEN_B)] == [push_kind]


def test_historical_attention_is_quiet(room: Room) -> None:
    room.attention("failure", historical=True)
    room.step()
    assert room.relay.sent == []


def test_an_opened_gate_pings_its_deciders_once(room: Room) -> None:
    room.api.ctx.chronology.record(
        "gate.opened",
        room.api.clock(),
        run_id="r_gate",
        actor=DAEMON_ACTOR,
        data={"kind": "merge", "state": "open", "pr_number": 7, "pr_url": None, "revision": 1},
    )
    # The same run, announced again through its conversation, is one ping.
    room.attention("action_required", run_id=run_public_id("r_gate"))
    room.step()
    pings = room.pushes_to(TOKEN_A)
    assert [p["k"] for p in pings] == ["gate"]
    notice = room.notification(pings[0]["ref"])
    assert notice["title"] == "Decision needed"
    assert room.pushes_to(TOKEN_B) == []


# -- preferences ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefs", "expected"),
    [
        ({}, ["mention", "work", "gate"]),
        ({"mentions": False}, ["work", "gate"]),
        ({"work": False}, ["mention", "gate"]),
        ({"gates": False}, ["mention", "work"]),
        ({"per_channel": {"CHANNEL": "none"}}, []),
        ({"per_channel": {"CHANNEL": "mentions"}}, ["mention"]),
        ({"per_channel": {"CHANNEL": "all"}}, ["mention", "work", "gate"]),
        ({"per_channel": {"chn_other": "none"}}, ["mention", "work", "gate"]),
    ],
)
def test_a_devices_preferences_choose_what_reaches_it(
    room: Room, prefs: dict[str, Any], expected: list[str]
) -> None:
    if "per_channel" in prefs:
        prefs = {
            "per_channel": {
                (room.channel_id if key == "CHANNEL" else key): value
                for key, value in prefs["per_channel"].items()
            }
        }
    room.prefs("owner", **prefs)
    room.say(room.bob, "@owner look")
    turn = room.say(room.owner, "fix it")
    _deliver(room, turn.id, "completed", "Fix", None)
    room.attention("action_required")
    room.step()
    assert [p["k"] for p in room.pushes_to(TOKEN_A)] == expected


def test_failures_have_their_own_switch(room: Room) -> None:
    room.prefs("owner", failures=False)
    turn = room.say(room.owner, "fix it")
    _deliver(room, turn.id, "failed", "Fix", None)
    room.step()
    assert room.pushes_to(TOKEN_A) == []


# -- live only --------------------------------------------------------------------------


def test_only_what_happens_after_the_dispatcher_starts_is_sent(
    tmp_path: Path, relay: FakeRelay
) -> None:
    api = push_api(tmp_path, relay)
    with api.client:
        owner_headers = register_owner(api)
        register_member(api)
        api.client.post("/v1/users/me/devices", json=device(TOKEN_A), headers=owner_headers)
        store = api.ctx.collaboration
        owner = store.user_by_username("owner")
        bob = store.user_by_username("bob")
        assert owner is not None and bob is not None
        channel = store.create_channel(owner.id, "Plans", api.clock())
        store.add_channel_member(owner.id, channel.id, bob.id, "member", api.clock())

        def mention() -> None:
            store.accept_turn(
                bob.id,
                channel.id,
                content="@owner hello",
                targets=(),
                client_turn_id=None,
                client_message_id=None,
                actor=None,
                now=api.clock(),
            )

        mention()
        # A restart primes a fresh dispatcher at the chronology's head: the
        # mention above is history and is never sent, however often it runs.
        dispatcher = api.ctx.push.dispatcher
        dispatcher.prime()
        dispatcher.step()
        dispatcher.step()
        assert relay.sent == []
        mention()
        dispatcher.step()
        dispatcher.step()
        assert len(relay.sent) == 1
    api.ctx.close()


def test_a_dispatcher_that_was_never_primed_sends_nothing(tmp_path: Path, relay: FakeRelay) -> None:
    api = push_api(tmp_path, relay)
    with api.client:
        headers = register_owner(api)
        register_member(api)
        api.client.post("/v1/users/me/devices", json=device(TOKEN_A), headers=headers)
        owner = api.ctx.collaboration.user_by_username("owner")
        assert owner is not None
        api.ctx.push.dispatcher.step()
        assert relay.sent == []
    api.ctx.close()


# -- when the relay balks ---------------------------------------------------------------


def _mention(room: Room) -> None:
    room.say(room.bob, "@owner ping")
    room.api.ctx.push.dispatcher.scan()


def test_an_upstream_failure_is_retried_with_exponential_backoff(room: Room) -> None:
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.fail_next(502, {"error": "upstream", "reason": "InternalServerError"}, times=3)
    _mention(room)
    attempts = lambda: len([r for r, _ in room.relay.requests if r == "/v1/push"])  # noqa: E731

    dispatcher.deliver_due()
    assert attempts() == 1 and room.relay.sent == []
    # Backoff doubles from `[push] backoff_s` (2 s): 2, then 4, then 8.
    for wait, expected in ((1, 1), (1, 2), (3, 2), (1, 3), (7, 3), (1, 4)):
        room.api.clock.t += wait
        dispatcher.deliver_due()
        assert attempts() == expected, (wait, expected)
    assert len(room.pushes_to(TOKEN_A)) == 1
    assert dispatcher.pending() == 0


def test_a_rate_limit_waits_as_long_as_the_relay_says(room: Room) -> None:
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.fail_next(429, {"error": "rate_limited"}, headers={"Retry-After": "30"})
    _mention(room)
    dispatcher.deliver_due()
    room.api.clock.t += 29
    dispatcher.deliver_due()
    assert room.relay.sent == []
    room.api.clock.t += 1
    dispatcher.deliver_due()
    assert len(room.relay.sent) == 1


def test_an_unreachable_relay_is_retried(room: Room) -> None:
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.unreachable()
    _mention(room)
    dispatcher.deliver_due()
    room.api.clock.t += 2
    dispatcher.deliver_due()
    assert len(room.relay.sent) == 1


def test_a_failure_the_relay_calls_final_is_not_retried(room: Room) -> None:
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.fail_next(502, {"error": "upstream", "reason": "BadTopic", "retryable": False})
    _mention(room)
    dispatcher.deliver_due()
    assert dispatcher.pending() == 0
    room.api.clock.t += 3600
    dispatcher.deliver_due()
    assert room.relay.sent == []


def test_retries_stop_after_max_attempts(room: Room, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.fail_next(502, {"error": "upstream", "reason": "x"}, times=50)
    _mention(room)
    for _ in range(20):
        dispatcher.deliver_due()
        room.api.clock.t += 600
    pushes = [r for r, _ in room.relay.requests if r == "/v1/push"]
    assert len(pushes) == 5  # `[push] max_attempts`
    assert dispatcher.pending() == 0
    assert "push.gave_up" in caplog.text


def test_an_unregistered_device_is_pruned(room: Room) -> None:
    room.relay.unregistered.add(TOKEN_A)
    _mention(room)
    room.api.ctx.push.dispatcher.deliver_due()
    assert room.api.ctx.push.dispatcher.pending() == 0
    listed = room.api.client.get("/v1/users/me/devices", headers=room.owner_headers).json()
    assert listed == {"items": []}
    # Bob's device is untouched.
    assert (
        len(room.api.client.get("/v1/users/me/devices", headers=room.bob_headers).json()["items"])
        == 1
    )


def test_a_refused_push_is_dropped_and_the_device_kept(room: Room) -> None:
    room.relay.fail_next(400, {"error": "invalid_request"})
    _mention(room)
    room.api.ctx.push.dispatcher.deliver_due()
    room.api.clock.t += 3600
    room.api.ctx.push.dispatcher.deliver_due()
    assert room.relay.sent == []
    assert room.api.ctx.push.dispatcher.pending() == 0
    assert len(room.api.ctx.push.devices.for_user(room.owner.id)) == 1


def test_a_handle_the_relay_no_longer_knows_is_re_enrolled_on_the_next_registration(
    room: Room,
) -> None:
    room.relay.fail_next(400, {"error": "invalid_handle"})
    _mention(room)
    room.api.ctx.push.dispatcher.deliver_due()
    enrolled = len([r for r, _ in room.relay.requests if r == "/v1/enroll"])
    # Until the app registers again the device has nothing to push with.
    room.say(room.bob, "@owner again")
    room.step()
    assert room.pushes_to(TOKEN_A) == []
    again = room.api.client.post(
        "/v1/users/me/devices", json=device(TOKEN_A), headers=room.owner_headers
    )
    assert again.status_code == 200
    assert len([r for r, _ in room.relay.requests if r == "/v1/enroll"]) == enrolled + 1
    room.say(room.bob, "@owner third time")
    room.step()
    assert len(room.pushes_to(TOKEN_A)) == 1


def test_old_notifications_are_pruned_with_the_chronology(room: Room) -> None:
    room.say(room.bob, "@owner ping")
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    retention = room.api.ctx.config.api.replay_retention_s
    room.api.clock.t += retention + 1
    assert room.api.ctx.push.notification(room.owner.id, ping["ref"]) is not None
    room.api.ctx.push.dispatcher.prune()
    assert room.api.ctx.push.notification(room.owner.id, ping["ref"]) is None


# -- planning (#2349) -------------------------------------------------------------------


def _plan_node(room: Room) -> None:
    """A plan with one epic, E-Checkout, for the notices to name."""
    store = PlanStore(room.api.harness.dstore)
    now = room.api.clock()
    epic = PlanNode(
        id="nod_epic",
        plan_id="pln_1",
        parent_id=None,
        position=0,
        level="epic",
        repository="o/r",
        state="published",
        origin="person",
        title="Checkout",
    )
    task = PlanNode(
        id="nod_task",
        plan_id="pln_1",
        parent_id="nod_epic",
        position=0,
        level="task",
        repository="o/r",
        state="published",
        origin="person",
        title="Store the cart",
    )
    store.create(
        Plan(
            id="pln_1",
            workspace_id="default",
            root_id="nod_epic",
            archived=False,
            created_by=None,
            created_by_display=None,
            created_at=now,
            updated_at=now,
            revision=1,
            nodes=(epic, task),
        ),
        events=[],
        actor=None,
    )


def _plan_item(
    room: Room, *, admitted_by: LocalUser | None = None, message_id: str | None = None
) -> str:
    """The ``plan`` run's work item, run ``run_plan``, admitted by an API
    operation of ``admitted_by`` or asked for by the chat ``message_id``."""
    item_id = "api:plan:pln_1-nod_epic"
    now = room.api.clock()
    with room.api.harness.dstore.transaction() as session:
        session.add(
            WorkItemRow(
                item_id=item_id,
                source_key="plan:pln_1-nod_epic",
                title="Break down Checkout",
                state="running",
                run_id="run_plan",
                run_kind="plan",
                repo="o/r",
                message_id=message_id,
                created_at=now,
                updated_at=now,
            )
        )
        if admitted_by is not None:
            session.add(
                OperationRow(
                    id="op_admit",
                    action="item.admit",
                    target_kind="item",
                    target_key=item_id,
                    state="succeeded",
                    actor_json=json.dumps({"kind": "client", "id": admitted_by.client_id}),
                    accepted_at=now,
                )
            )
    return item_id


def _plan_event(
    room: Room,
    type_: str,
    data: dict[str, Any],
    *,
    item_id: str | None = None,
    run_id: str | None = None,
    channel_id: str | None = None,
    actor: dict[str, Any] | None = None,
) -> None:
    now = room.api.clock()
    with room.api.harness.dstore.transaction() as session:
        session.add(
            ApiEventRow(
                recorded_at=now,
                occurred_at=now,
                type=type_,
                run_id=run_id,
                item_id=item_id,
                actor_json=json.dumps(actor) if actor is not None else None,
                data_json=json.dumps(data),
                channel_id=channel_id,
            )
        )


def _questions(room: Room, item_id: str, channel_id: str | None = None) -> None:
    _plan_event(
        room,
        "plan.generation.questions",
        {
            "plan_id": "pln_1",
            "node_id": "nod_epic",
            "run_id": run_public_id("run_plan"),
            "questions": [{"id": "q1"}, {"id": "q2"}],
        },
        item_id=item_id,
        run_id="run_plan",
        channel_id=channel_id,
    )


def _proposed(room: Room, item_id: str | None) -> None:
    _plan_event(
        room,
        "plan.generation.proposed",
        {
            "plan_id": "pln_1",
            "node_id": "nod_epic",
            "run_id": run_public_id("run_plan"),
            "count": 3,
        },
        item_id=item_id,
        run_id="run_plan",
    )


def test_breakdown_notices_go_only_to_the_person_who_asked(room: Room) -> None:
    _plan_node(room)
    item_id = _plan_item(room, admitted_by=room.owner)
    _questions(room, item_id, channel_id=room.channel_id)
    _proposed(room, None)  # found through its run
    room.step()

    questions, proposal = room.pushes_to(TOKEN_A)
    assert (questions["k"], questions["thread"]) == ("gate", room.channel_id)
    assert (proposal["k"], proposal["thread"]) == ("work", "")
    first = room.notification(questions["ref"])
    assert (first["title"], first["body"]) == (
        "Questions waiting for you",
        "The planner has 2 questions about Checkout before it proposes its tasks.",
    )
    second = room.notification(proposal["ref"])
    assert (second["title"], second["body"]) == (
        "A proposal is ready for you",
        "The planner proposed 3 tasks for Checkout. Review and approve it.",
    )
    # Bob is in the channel and in the workspace: neither is his to hear.
    assert room.pushes_to(TOKEN_B) == []


def test_a_breakdown_asked_for_in_the_chat_goes_to_the_messages_author(room: Room) -> None:
    _plan_node(room)
    turn = room.say(room.bob, "break the checkout epic down")
    item_id = _plan_item(room, message_id=turn.input_message_id)
    _proposed(room, item_id)
    room.step()
    [ping] = room.pushes_to(TOKEN_B)
    assert ping["k"] == "work"
    assert room.pushes_to(TOKEN_A) == []


def test_a_breakdown_nobody_asked_for_is_not_broadcast(room: Room) -> None:
    _plan_node(room)
    item_id = _plan_item(room)  # a host-trusted operator, say: no member
    _questions(room, item_id)
    _proposed(room, item_id)
    room.step()
    assert room.relay.sent == []


def test_breakdown_notices_answer_to_the_gates_and_work_switches(room: Room) -> None:
    _plan_node(room)
    item_id = _plan_item(room, admitted_by=room.owner)
    room.prefs("owner", gates=False)
    _questions(room, item_id)
    _proposed(room, item_id)
    room.step()
    assert [p["k"] for p in room.pushes_to(TOKEN_A)] == ["work"]
    room.prefs("owner", gates=True, work=False)
    _questions(room, item_id)
    _proposed(room, item_id)
    room.step()
    assert [p["k"] for p in room.pushes_to(TOKEN_A)] == ["work", "gate"]


def _epic_run(room: Room, started_by: LocalUser) -> None:
    run = EpicRun(
        id="erun_1",
        plan_id="pln_1",
        node_id="nod_epic",
        state="running",
        started_by=started_by.client_id,
        started_by_display=started_by.username,
        created_at=room.api.clock(),
        updated_at=room.api.clock(),
    )
    EpicRunStore(room.api.harness.dstore).create(run, events=[], actor={})


def _paused(room: Room, reason: str, *, actor: LocalUser | None = None) -> None:
    ids = {"plan_id": "pln_1", "node_id": "nod_epic", "epic_run_id": "erun_1"}
    if reason == "task_failed":
        data = {
            **ids,
            "reason": "task_failed",
            "state": "running",
            "task_node_id": "nod_task",
            "item_id": "gh:issue:11",
            "error": "tests failed",
            "blocked": [],
        }
    else:
        data = {**ids, "reason": "person", "state": "paused", "by": "Owner"}
    who = None if actor is None else {"kind": "client", "id": actor.client_id}
    _plan_event(room, "plan.run.paused", data, actor=who)


def test_a_paused_epic_run_pings_only_the_person_who_started_it(room: Room) -> None:
    _plan_node(room)
    _epic_run(room, started_by=room.bob)
    _paused(room, "task_failed")
    _paused(room, "person", actor=room.owner)
    room.step()

    failed, paused = room.pushes_to(TOKEN_B)
    assert (failed["k"], paused["k"]) == ("failure", "failure")
    first = room.notification(failed["ref"], who="bob")
    assert (first["title"], first["body"]) == (
        "Your epic run needs you",
        "Store the cart failed in Checkout: tests failed. "
        "Retry or skip it; what depends on it waits.",
    )
    second = room.notification(paused["ref"], who="bob")
    assert (second["title"], second["body"]) == (
        "Your epic run was paused",
        "Owner paused Checkout. Nothing new starts until it is resumed.",
    )
    assert room.pushes_to(TOKEN_A) == []


def test_pausing_your_own_epic_run_is_not_news(room: Room) -> None:
    _plan_node(room)
    _epic_run(room, started_by=room.bob)
    _paused(room, "person", actor=room.bob)
    room.step()
    assert room.relay.sent == []


def test_a_paused_epic_run_answers_to_the_failures_switch(room: Room) -> None:
    _plan_node(room)
    _epic_run(room, started_by=room.bob)
    room.prefs("bob", failures=False)
    _paused(room, "task_failed")
    room.step()
    assert room.relay.sent == []


# -- reminders --------------------------------------------------------------------------

TOKEN_C = "c3" * 32


def _remind(
    room: Room,
    *,
    group: str,
    entry_id: str,
    capabilities: list[str],
    title: str = "Fix the login",
    run_id: str | None = None,
    item_id: str | None = None,
    reminders: int = 1,
    waiting_s: int = 14400,
    **data: Any,
) -> None:
    """An ``attention.reminder`` as the tracker records one: the opening's
    data plus how long and how often, scoped to the entry's run and item."""
    kind, state = {
        "decision": ("gate", "gated"),
        "failed": ("item", "blocked"),
        "paused": ("provider_hold", "provider_held"),
    }[group]
    room.api.ctx.chronology.record(
        "attention.reminder",
        room.api.clock(),
        run_id=run_id,
        item_id=item_id,
        actor=DAEMON_ACTOR,
        data={
            "entry_id": entry_id,
            "kind": kind,
            "group": group,
            "state": state,
            "title": title,
            "since": "2026-10-01T12:00:00Z",
            "repository": "o/r",
            "repository_id": "repo_1",
            "item_id": "itm_1" if item_id else None,
            "run_id": run_public_id(run_id) if run_id else None,
            "gate_id": None,
            "plan_id": None,
            "node_id": None,
            "epic_run_id": None,
            "revision": 3,
            "waiting_s": waiting_s,
            "reminders": reminders,
            "capabilities": capabilities,
            **data,
        },
    )


def _blocked_in_channel(room: Room, channel_id: str | None) -> str:
    """A blocked item the channel asked for (none: nobody did); its id as
    the store keeps it, which is what the tracker scopes an event by."""
    dstore = room.api.harness.dstore
    fields = {} if channel_id is None else {"channel_id": channel_id}
    dstore.upsert_new(gh_item("1", **fields), room.api.clock())
    dstore.mark_blocked("gh:1", "stuck", room.api.clock())
    return next(item.item_id for item in dstore.items() if item.source_key == "1")


def _admin(room: Room) -> None:
    """A third person, an admin, with a device: can approve, is not in
    the room's channel."""
    headers = register_member(room.api, "ann", "admin")
    added = room.api.client.post("/v1/users/me/devices", json=device(TOKEN_C), headers=headers)
    assert added.status_code in (200, 201), added.text


def test_a_decision_reminder_goes_to_who_can_approve_and_see_the_channel(room: Room) -> None:
    _admin(room)
    item_id = _blocked_in_channel(room, room.channel_id)
    _remind(
        room,
        group="decision",
        entry_id="gate:gate_1",
        capabilities=["gates:approve", "runs:control"],
        item_id=item_id,
        waiting_s=4 * 3600,
    )
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    assert ping["k"] == "gate" and ping["thread"] == room.channel_id
    notice = room.notification(ping["ref"])
    assert notice["kind"] == "gate" and notice["channel_id"] == room.channel_id
    assert notice["title"] == "Still waiting: Fix the login"
    assert notice["body"] == "A decision has been waiting 4 hours. First reminder."
    # Bob can see the channel but cannot approve; Ann can approve but
    # cannot see the channel.
    assert room.pushes_to(TOKEN_B) == [] and room.pushes_to(TOKEN_C) == []


def test_a_failed_item_reminder_goes_to_who_can_act_on_it(room: Room) -> None:
    _admin(room)
    item_id = _blocked_in_channel(room, None)
    _remind(
        room,
        group="failed",
        entry_id="item:itm_1:blocked:run_r1",
        capabilities=["runs:control"],
        item_id=item_id,
        run_id="r1",
        reminders=2,
        waiting_s=2 * 86400 + 3600,
    )
    room.step()
    # Work no channel asked for is the owners' and admins'.
    for token in (TOKEN_A, TOKEN_C):
        [ping] = room.pushes_to(token)
        assert ping["k"] == "failure" and ping["thread"] == ""
    notice = room.notification(room.pushes_to(TOKEN_A)[0]["ref"])
    assert notice["title"] == "Still waiting: Fix the login"
    assert notice["body"] == "It ended blocked 2 days ago and still needs someone. Reminder 2."
    assert room.pushes_to(TOKEN_B) == []


def test_a_reminder_nobody_below_owner_can_act_on_goes_to_the_owners(room: Room) -> None:
    _admin(room)
    _remind(
        room,
        group="paused",
        entry_id="provider_hold:claude:3",
        capabilities=[],
        title="The claude provider is held until someone recovers it",
        waiting_s=90 * 60,
    )
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    assert ping["k"] == "failure"
    notice = room.notification(ping["ref"])
    assert notice["title"] == "Still waiting: The claude provider is held until someone recovers it"
    assert notice["body"] == (
        "It has been held 1 hour; nothing moves until someone clears it. First reminder."
    )
    assert room.pushes_to(TOKEN_B) == [] and room.pushes_to(TOKEN_C) == []


def test_a_suspended_repository_reminder_goes_to_who_can_resume_it(room: Room) -> None:
    _admin(room)
    _remind(
        room,
        group="paused",
        entry_id="repository:repo_1",
        capabilities=["daemon:manage"],
        title="o/r is no longer polled for work",
        kind="repository",
        state="suspended",
    )
    room.step()
    assert [p["k"] for p in room.pushes_to(TOKEN_A)] == ["failure"]
    assert [p["k"] for p in room.pushes_to(TOKEN_C)] == ["failure"]
    assert room.pushes_to(TOKEN_B) == []


@pytest.mark.parametrize(
    ("group", "prefs", "expected"),
    [
        ("decision", {"gates": False}, []),
        ("decision", {"failures": False}, ["gate"]),
        ("failed", {"failures": False}, []),
        ("failed", {"gates": False}, ["failure"]),
        ("paused", {"failures": False}, []),
    ],
)
def test_a_reminder_answers_to_the_switch_of_its_kind(
    room: Room, group: str, prefs: dict[str, Any], expected: list[str]
) -> None:
    room.prefs("owner", **prefs)
    _remind(room, group=group, entry_id=f"{group}:x", capabilities=["gates:approve"])
    room.step()
    assert [p["k"] for p in room.pushes_to(TOKEN_A)] == expected


def test_a_reminder_in_a_muted_channel_is_not_sent(room: Room) -> None:
    room.prefs("owner", per_channel={room.channel_id: "none"})
    item_id = _blocked_in_channel(room, room.channel_id)
    _remind(
        room,
        group="decision",
        entry_id="gate:gate_1",
        capabilities=["gates:approve"],
        item_id=item_id,
    )
    room.step()
    assert room.pushes_to(TOKEN_A) == []


def test_two_successive_reminders_are_both_delivered_and_the_same_one_never_twice(
    room: Room,
) -> None:
    _remind(room, group="decision", entry_id="gate:gate_1", capabilities=["gates:approve"])
    room.step()
    # The same reminder read again (a replayed row) is one ping.
    _remind(room, group="decision", entry_id="gate:gate_1", capabilities=["gates:approve"])
    room.step()
    assert len(room.pushes_to(TOKEN_A)) == 1
    # The next reminder, within the dispatcher's dedupe hour, is news.
    room.api.clock.t += 60
    _remind(
        room, group="decision", entry_id="gate:gate_1", capabilities=["gates:approve"], reminders=2
    )
    room.step()
    pings = room.pushes_to(TOKEN_A)
    assert len(pings) == 2
    first, second = (room.notification(p["ref"]) for p in pings)
    assert first["body"].endswith("First reminder.") and second["body"].endswith("Reminder 2.")


def test_a_reminder_without_an_entry_or_a_title_is_quiet(room: Room) -> None:
    _remind(room, group="decision", entry_id="", capabilities=["gates:approve"])
    _remind(room, group="decision", entry_id="gate:g", capabilities=["gates:approve"], title="")
    room.step()
    assert room.relay.sent == []


def test_a_reminder_the_tracker_records_reaches_a_device(tmp_path: Path, relay: FakeRelay) -> None:
    """End to end: the tracker's own event, as it writes it, is what the
    rule reads."""
    api = build(
        tmp_path,
        config={
            "push": {"enabled": True, "relay_url": RELAY},
            "attention": {"remind_after_s": 300, "remind_every_s": 300},
        },
    )
    api.ctx.relay_transport = relay.transport
    with api.client:
        owner_headers = register_owner(api)
        api.client.post("/v1/users/me/devices", json=device(TOKEN_A), headers=owner_headers)
        api.ctx.push.dispatcher.prime()
        assert _step(api) == 0
        _blocked(api)
        assert _step(api) == 1
        api.ctx.push.dispatcher.step()
        # The opening itself is not a push.
        assert relay.sent == []
        api.clock.t += 300
        assert _step(api) == 1
        api.ctx.push.dispatcher.step()
        [ping] = [sent.payload for sent in relay.sent if sent.token == TOKEN_A]
        assert ping["k"] == "failure"
        notice = api.client.get(
            f"/v1/users/me/notifications/{ping['ref']}", headers=owner_headers
        ).json()
        assert notice["title"] == "Still waiting: Do 1"
        assert notice["body"] == (
            "It ended blocked 5 minutes ago and still needs someone. First reminder."
        )
        api.clock.t += SWEEP_S
        assert _step(api) == 0
        api.ctx.push.dispatcher.step()
        assert len(relay.sent) == 1
    api.ctx.close()
