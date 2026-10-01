"""A parked plan run's questions, answered from its chat thread (#2345).

When a plan run asks before it proposes, its thread on each chat bridge
gets the questions as clickable choices — the same seam every clarifying
question uses (``_send_choices``: buttons where the service has them,
numbered prose where not). A click on a choice, or a reply in the thread
(a number, a choice's name, the person's own words, or ``skip``), answers
the plan through the same loop path the API's answers route takes, and the
run goes back to the queue once the questions are settled. A reply is
matched against the plan record itself, so it still answers after a
restart has emptied the bridge's memory of what it posted.

These tests drive the Discord bridge over the fakes its own suite uses,
with a real daemon loop and plan service behind it; the run is parked the
way the daemon parks one (item ``awaiting_answers``, run pinned, questions
on the node) without spending a sandbox to get there.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from lantern import hostgit
from lantern.config import Config
from lantern.daemon.controls.intake import PlanAdmission, plan_item
from lantern.daemon.discord import DiscordBridge
from lantern.engine.planning import PlanQuestion
from lantern.events import HostEventTypes
from lantern.ghids import api_item_id
from lantern_worker.protocol import Event
from tests.conftest import FakeSbx
from tests.fakes.gitrepo import make_repo
from tests.unit.test_daemon_discord import (
    BOT_USER,
    FakeChannel,
    FakeClient,
    FakeMessage,
    steer_msg,
    wait_for,
)
from tests.unit.test_engine import Harness
from tests.unit.test_engine_plan import question
from tests.unit.test_plan_generation import World

RUN = "rclarify1"
THREAD = 421
QUESTIONS = (
    question("fmt", "Which formats?", "csv", "pdf"),
    question("who", "Who downloads them?", "staff", "public", free=False),
)


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    origin = make_repo(tmp_path, "upstream", {"README.md": "# app\n"})
    monkeypatch.setattr(
        hostgit,
        "clone_from_remote",
        lambda url, target, branch, **kw: hostgit.clone_for_run(origin, target, branch),
    )
    return Harness(fake_sbx, tmp_path, monkeypatch)


def park(world: World, *questions: dict[str, Any]) -> str:
    """A breakdown parked on ``questions`` as the daemon leaves one; the
    item id."""
    item = plan_item(
        world.loop,
        PlanAdmission(world.plan_id, world.epic_id, expected_revision=world.revision),
        item_id=api_item_id("plan:k1"),
    )
    world.dstore.upsert_new(item, 5.0)
    world.dstore.mark_running(item.item_id, RUN, 5.5)
    world.dstore.mark_awaiting_answers(item.item_id, 6.0)
    world.plans.ask_questions(
        world.plan_id,
        world.epic_id,
        [PlanQuestion.model_validate(q) for q in questions or QUESTIONS],
        run_id=RUN,
        now=7.0,
        item_id=item.item_id,
    )
    return item.item_id


def bridge_for(world: World, tmp_path: Path) -> tuple[DiscordBridge, FakeChannel]:
    """A Discord bridge over the world's store and loop, with the run's
    thread already open (as ``_ensure_thread`` left it before the park)."""
    config = Config.model_validate(
        {"home": str(tmp_path / "chat-home"), "discord": {"channel_id": 42}}
    )
    client = FakeClient(42)

    def factory(bridge: DiscordBridge) -> FakeClient:
        client.bridge = bridge
        return client

    bridge = DiscordBridge(
        config, world.dstore, loop_ref=world.loop, client_factory=factory, token="tok"
    )
    thread = FakeChannel(client, THREAD, name="run thread")
    client.channels[THREAD] = thread
    world.dstore.record_chat_thread(RUN, "42", str(THREAD), None, backend="discord")
    bridge.start()
    return bridge, thread


def post_questions(bridge: DiscordBridge, thread: FakeChannel, world: World) -> None:
    event = Event(
        ts=1.0,
        run_id=RUN,
        type=HostEventTypes.RUN_AWAITING_ANSWERS,
        data={"plan_id": world.plan_id, "node_id": world.epic_id, "questions": list(QUESTIONS)},
    )
    assert bridge._aloop is not None
    asyncio.run_coroutine_threadsafe(
        bridge._post_plan_questions(RUN, thread, event), bridge._aloop
    ).result(timeout=10)


def waiting(world: World) -> Any:
    found = world.plans.get(world.plan_id).node(world.epic_id).generation
    assert found is not None
    return found


def posted(thread: FakeChannel, text: str) -> FakeMessage:
    return next(m for m in thread.messages.values() if text in m.content)


def test_questions_post_as_choices_and_a_click_then_a_reply_answer_them(
    harness: Harness, tmp_path: Path
) -> None:
    world = World(harness)
    item_id = park(world)
    bridge, thread = bridge_for(world, tmp_path)
    try:
        post_questions(bridge, thread, world)
        # One intro, then each question with its choices numbered (the
        # prose every bridge carries under its buttons).
        assert "The planner asks 2 questions" in thread.sent[0]
        assert "1/2 · Which formats?" in thread.sent[1] and "1. CSV — about csv" in thread.sent[1]
        assert "2/2 · Who downloads them?" in thread.sent[2]
        assert "Reply with a number or option name." in thread.sent[2]
        first = posted(thread, "Which formats?")
        second = posted(thread, "Who downloads them?")

        # A click answers the first; the run keeps waiting for the second.
        assert bridge._answer_choice(str(first.id), "pdf", "brett", author_name="brett")
        assert waiting(world).answers["fmt"].value == "pdf"
        assert waiting(world).status == "awaiting_answers"
        assert wait_for(lambda: any("1 question still open" in s for s in thread.sent))
        item = world.dstore.get(item_id)
        assert item is not None and item.state == "awaiting_answers"
        # A second click on the same question is no longer a plan question.
        assert bridge._answer_choice(str(first.id), "csv", "brett") is False

        # A reply to the second question names a choice by its number.
        reply = FakeMessage("2", thread, mid=901, reply_to=second)
        thread.messages[901] = reply
        bridge._handle_message(reply)
        settled = waiting(world)
        assert settled.status == "answered" and settled.answers["who"].value == "public"
        assert settled.answered_by == "brett"
        item = world.dstore.get(item_id)
        assert item is not None and item.state == "queued" and item.run_id == RUN
        assert wait_for(lambda: any("all answered" in s for s in thread.sent))
        assert wait_for(lambda: "✅" in reply.reactions)
        # The same record the API writes: the answered event, scoped to the run.
        (answered,) = world.events("plan.generation.answered")
        assert answered.run_id == RUN and answered.item_id == item_id
    finally:
        bridge.close()


def test_a_reply_answers_after_a_restart_emptied_the_bridge(
    harness: Harness, tmp_path: Path
) -> None:
    """A fresh bridge never posted the questions: a mention in the thread
    still answers the one question left open, in the person's own words,
    from the plan record."""
    world = World(harness)
    item_id = park(world, question("fmt", "Which formats?", "csv", "pdf"))
    bridge, thread = bridge_for(world, tmp_path)
    try:
        assert bridge._plan_questions == {}
        msg = steer_msg("spreadsheets, and a printable one", thread, mid=902)
        bridge._handle_message(msg)
        settled = waiting(world)
        assert settled.status == "answered"
        assert settled.answers["fmt"].text == "spreadsheets, and a printable one"
        item = world.dstore.get(item_id)
        assert item is not None and item.state == "queued"
        assert not any("has finished" in s for s in thread.sent)
    finally:
        bridge.close()


def test_skip_in_the_thread_lets_the_planner_decide(harness: Harness, tmp_path: Path) -> None:
    world = World(harness)
    item_id = park(world)
    bridge, thread = bridge_for(world, tmp_path)
    try:
        bridge._handle_message(steer_msg("skip", thread, mid=903))
        assert waiting(world).status == "skipped"
        item = world.dstore.get(item_id)
        assert item is not None and item.state == "queued"
        assert wait_for(lambda: any("skipped the questions" in s for s in thread.sent))
    finally:
        bridge.close()


def test_an_ambiguous_or_unmatched_reply_is_not_guessed_at(
    harness: Harness, tmp_path: Path
) -> None:
    world = World(harness)
    park(world)
    bridge, thread = bridge_for(world, tmp_path)
    try:
        # Two questions open and the reply names neither: asked to say which.
        bridge._handle_message(steer_msg("csv", thread, mid=904))
        assert wait_for(lambda: any("2 questions are still open" in s for s in thread.sent))
        assert waiting(world).answers == {}
        post_questions(bridge, thread, world)
        who = posted(thread, "Who downloads them?")
        # A question that takes only its choices does not take prose.
        prose = FakeMessage("everyone, really", thread, mid=905, reply_to=who)
        thread.messages[905] = prose
        bridge._handle_message(prose)
        assert wait_for(lambda: any("takes one of its choices" in s for s in thread.sent))
        assert waiting(world).answers == {}
    finally:
        bridge.close()


def test_a_thread_whose_run_is_not_waiting_still_says_it_finished(
    harness: Harness, tmp_path: Path
) -> None:
    world = World(harness)
    item_id = park(world)
    world.dstore._update(item_id, 8.0, state="done")
    bridge, thread = bridge_for(world, tmp_path)
    try:
        bridge._handle_message(steer_msg("csv", thread, mid=906))
        assert wait_for(lambda: any("has finished" in s for s in thread.sent))
        assert waiting(world).answers == {}
    finally:
        bridge.close()


def test_the_bot_is_the_one_addressed(harness: Harness, tmp_path: Path) -> None:
    """Plain thread chatter is not an answer: it neither mentions the bot
    nor replies to it, so routing ignores it as it ignores any."""
    world = World(harness)
    park(world, question("fmt", "Which formats?", "csv", "pdf"))
    bridge, thread = bridge_for(world, tmp_path)
    try:
        chatter = FakeMessage("csv", thread, mid=907)
        thread.messages[907] = chatter
        bridge._handle_message(chatter)
        assert waiting(world).answers == {}
        assert BOT_USER.id not in {m.id for m in chatter.mentions}
    finally:
        bridge.close()
