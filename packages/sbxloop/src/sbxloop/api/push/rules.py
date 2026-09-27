"""Which chronology events are news, and for whom.

The same rules a chat client applies before showing a notice, decided here
with what the daemon knows first-hand — who wrote a message, who asked for
a turn, who may decide a gate — instead of what a client can infer:

- **mention**: a person's message (never the recipient's own) that
  addresses the recipient by ``@username``, word-bounded and
  case-insensitively, in a channel the recipient can open.
- **work**: work delivered, or a reply finished, for a turn the recipient
  asked for; a job's ``work`` attention to the channel's members.
- **failure**: the same when it ended failed, blocked, cancelled or
  abandoned, or the reply failed; a job's ``failure`` attention.
- **gate**: a job's ``action_required`` attention, or a merge gate
  opening, to the people who may approve it and can see where it is.

Planning (#2349) adds three notices, each for **one person** and nobody
else — never the rest of the workspace, never the channel:

- **questions waiting for you** (``plan.generation.questions``, pushed as
  ``gate``: the planner is waiting on your answers) and **a proposal ready
  for you** (``plan.generation.proposed``, pushed as ``work``: the level
  you asked for arrived), to the person who asked for the breakdown — the
  author of the chat message behind the ``plan`` run's item, else the
  person whose ``item.admit`` operation queued it;
- **an epic run you started paused** (``plan.run.paused``, pushed as
  ``failure``), to the person who started the epic run — for a task that
  failed, or for someone else pausing it (never for your own pause).

They ride the existing kinds, so a device's ``gates``, ``work`` and
``failures`` switches govern them and the relay, which accepts only those
kinds, carries them unchanged.

Historical events (a job imported from before the daemon knew it) are
never news. :func:`allowed` then narrows by a device's own preferences.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from sbxloop.api.collaboration import CollaborationStore, Member, _message
from sbxloop.api.publicids import parse_run_id, run_public_id
from sbxloop.daemon.controls.principal import ROLE_CAPABILITIES
from sbxloop.db.api_models import OperationRow
from sbxloop.db.collaboration_models import (
    ChannelMemberRow,
    ChannelRow,
    MessageRow,
    TurnRow,
)
from sbxloop.db.daemon_models import PlanEpicRunRow, PlanNodeRow, WorkItemRow

MESSAGE_CREATED = "collaboration.message.created"
WORK_DELIVERED = "collaboration.work.delivered"
TURN_COMPLETED = "collaboration.turn.completed"
TURN_FAILED = "collaboration.turn.failed"
ATTENTION = "collaboration.external_work.attention"
GATE_OPENED = "gate.opened"
PLAN_QUESTIONS = "plan.generation.questions"
PLAN_PROPOSED = "plan.generation.proposed"
PLAN_PAUSED = "plan.run.paused"
#: Every event type a notice can come from.
TYPES: frozenset[str] = frozenset(
    {
        MESSAGE_CREATED,
        WORK_DELIVERED,
        TURN_COMPLETED,
        TURN_FAILED,
        ATTENTION,
        GATE_OPENED,
        PLAN_QUESTIONS,
        PLAN_PROPOSED,
        PLAN_PAUSED,
    }
)

#: A work state that means the work did not get done.
FAILED_STATES = frozenset({"failed", "blocked", "cancelled", "abandoned"})
#: The device preference each kind answers to.
KIND_PREF: dict[str, str] = {
    "mention": "mentions",
    "gate": "gates",
    "work": "work",
    "failure": "failures",
}
#: How a job's attention kind is pushed.
ATTENTION_KINDS: dict[str, str] = {
    "work": "work",
    "failure": "failure",
    "action_required": "gate",
}
#: A notification body is cut to this many characters, ellipsis included.
BODY_LIMIT = 140
#: A username longer than this is not one.
HANDLE_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Event:
    """One chronology row, as the rules read it."""

    seq: int
    type: str
    channel_id: str | None
    run_id: str | None
    item_id: str | None
    data: Mapping[str, Any] = field(default_factory=dict)
    #: Who the event is attributed to (its ``actor``), when anyone.
    actor: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Notice:
    """News for one person."""

    user_id: str
    kind: str
    channel_id: str | None
    turn_id: str | None
    title: str
    body: str
    #: Two notices with the same key for one person are one notice (a gate
    #: announced both as it opens and through its conversation).
    dedupe: str | None = None


def mentions(text: str, username: str) -> bool:
    """Whether ``text`` addresses ``username`` as ``@username``."""
    handle = username.strip()
    if not handle or len(handle) > HANDLE_LIMIT:
        return False
    pattern = rf"(^|[^\w@.])@{re.escape(handle)}(?![\w-])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def excerpt(text: str) -> str:
    """``text`` on one line, short enough for a notification body."""
    flat = " ".join(text.split())
    return flat if len(flat) <= BODY_LIMIT else flat[: BODY_LIMIT - 1] + "…"


def allowed(prefs: Mapping[str, Any], kind: str, channel_id: str | None) -> bool:
    """Whether a device's preferences let a notice of ``kind`` through."""
    if kind == "test":
        return True
    if not prefs.get(KIND_PREF.get(kind, kind), True):
        return False
    per_channel = prefs.get("per_channel") or {}
    mode = per_channel.get(channel_id, "all") if channel_id else "all"
    if mode == "none":
        return False
    if mode == "mentions":
        return kind == "mention"
    return True


def _can_decide(member: Member) -> bool:
    return "gates:approve" in ROLE_CAPABILITIES[member.role]


class NoticeRules:
    """Turns chronology events into notices. ``agent_name`` names an agent
    by slug (``None``: the default assistant) the way the chat does."""

    def __init__(self, agent_name: Callable[[str | None], str]) -> None:
        self.agent_name = agent_name

    def notices(self, session: Any, event: Event) -> list[Notice]:
        if event.type == MESSAGE_CREATED:
            return self._mention(session, event)
        if event.type == WORK_DELIVERED:
            return self._delivered(session, event)
        if event.type in (TURN_COMPLETED, TURN_FAILED):
            return self._turn(session, event)
        if event.type == ATTENTION:
            return self._attention(session, event)
        if event.type == GATE_OPENED:
            return self._gate(session, event)
        if event.type in (PLAN_QUESTIONS, PLAN_PROPOSED):
            return self._breakdown(session, event)
        if event.type == PLAN_PAUSED:
            return self._paused(session, event)
        return []

    # -- who ---------------------------------------------------------------------

    @staticmethod
    def _members(session: Any) -> list[Member]:
        return [m for m in CollaborationStore._join(session) if m.user.active]

    @staticmethod
    def _channel(session: Any, channel_id: str | None) -> ChannelRow | None:
        if not channel_id:
            return None
        row = session.get(ChannelRow, channel_id)
        if row is None or row.deleted_at is not None or row.state != "active":
            return None
        return row  # type: ignore[no-any-return]

    @staticmethod
    def _joined(session: Any, channel_id: str) -> set[str]:
        return set(
            session.scalars(
                select(ChannelMemberRow.user_id).where(ChannelMemberRow.channel_id == channel_id)
            )
        )

    def _viewers(self, session: Any, channel: ChannelRow) -> list[Member]:
        """The active members who can open ``channel``."""
        joined = self._joined(session, str(channel.id))
        return [
            member
            for member in self._members(session)
            if member.workspace_id == channel.workspace_id
            and (channel.visibility == "workspace" or member.user.id in joined)
        ]

    def _asker(self, session: Any, turn_id: str | None) -> str | None:
        """The person who asked for a turn: its author, or for a turn
        recorded before authors were, the author of the message that opened
        it. An agent's follow-up turn was asked by nobody."""
        if not turn_id:
            return None
        turn = session.get(TurnRow, turn_id)
        if turn is None:
            return None
        if turn.author_kind is not None:
            return str(turn.author_id) if turn.author_kind == "human" and turn.author_id else None
        opening = session.get(MessageRow, turn.input_message_id)
        if opening is None:
            return None
        author = _message(session, opening).author
        return author.id if author.kind == "human" else None

    def _visible_asker(self, session: Any, event: Event, turn_id: str | None) -> str | None:
        channel = self._channel(session, event.channel_id)
        asker = self._asker(session, turn_id)
        if channel is None or asker is None:
            return None
        if all(member.user.id != asker for member in self._viewers(session, channel)):
            return None
        return asker

    def _turn_agent(self, session: Any, turn_id: str | None) -> str:
        turn = session.get(TurnRow, turn_id) if turn_id else None
        targets = json.loads(turn.targets_json or "[]") if turn is not None else []
        slug = next((str(t) for t in targets if isinstance(t, str) and t), None)
        return self.agent_name(slug)

    # -- what --------------------------------------------------------------------

    def _mention(self, session: Any, event: Event) -> list[Notice]:
        if event.data.get("author_kind") != "human":
            return []
        channel = self._channel(session, event.channel_id)
        row = session.get(MessageRow, str(event.data.get("message_id") or ""))
        if channel is None or row is None:
            return []
        message = _message(session, row)
        author = message.author
        if author.kind != "human":
            return []
        name = author.display_name or "Someone"
        body = excerpt(message.content) or "They mentioned you in the chat."
        return [
            Notice(
                user_id=member.user.id,
                kind="mention",
                channel_id=message.channel_id,
                turn_id=message.turn_id,
                title=f"{name} mentioned you",
                body=body,
            )
            for member in self._viewers(session, channel)
            if member.user.id != author.id and mentions(message.content, member.user.username)
        ]

    def _delivered(self, session: Any, event: Event) -> list[Notice]:
        turn_id = str(event.data.get("turn_id") or "") or None
        asker = self._visible_asker(session, event, turn_id)
        row = session.get(MessageRow, str(event.data.get("message_id") or ""))
        if asker is None or row is None:
            return []
        work = json.loads(row.work_json) if row.work_json else {}
        work = work if isinstance(work, dict) else {}
        state = str(work.get("state") or "")
        label = str(work.get("title") or "") or "your work"
        slug = work.get("agent_slug") or row.agent_slug
        agent = self.agent_name(str(slug) if slug else None)
        if state in FAILED_STATES:
            kind, title = "failure", f"{agent} could not finish {label}"
            body = f"The run ended {state}. The details are in the chat."
        else:
            kind, title = "work", f"{agent} delivered {label}"
            body = (
                "Changes merged. The result is in the chat."
                if state == "merged"
                else "The result is in the chat."
            )
        return [Notice(asker, kind, event.channel_id, turn_id, title, body)]

    def _turn(self, session: Any, event: Event) -> list[Notice]:
        turn_id = str(event.data.get("turn_id") or "") or None
        asker = self._visible_asker(session, event, turn_id)
        if asker is None:
            return []
        agent = self._turn_agent(session, turn_id)
        if event.type == TURN_FAILED:
            error = str(event.data.get("error") or "").strip()
            return [
                Notice(
                    asker,
                    "failure",
                    event.channel_id,
                    turn_id,
                    f"{agent} could not reply",
                    excerpt(error) if error else f"{agent} could not finish that reply.",
                )
            ]
        return [
            Notice(
                asker,
                "work",
                event.channel_id,
                turn_id,
                f"{agent} replied",
                "A new reply is waiting in the chat.",
            )
        ]

    def _attention(self, session: Any, event: Event) -> list[Notice]:
        data = event.data
        kind = ATTENTION_KINDS.get(str(data.get("kind") or ""))
        title = data.get("title")
        if data.get("historical") is not False or kind is None:
            return []
        if not isinstance(title, str) or not title:
            return []
        channel = self._channel(session, event.channel_id)
        if channel is None:
            return []
        viewers = self._viewers(session, channel)
        if kind == "gate":
            recipients = [m.user.id for m in viewers if _can_decide(m)]
        else:
            # The people in the conversation, not everyone who could open it.
            joined = self._joined(session, str(channel.id)) | {str(channel.user_id)}
            recipients = [m.user.id for m in viewers if m.user.id in joined]
        body = data.get("body")
        run_id = data.get("run_id")
        dedupe = f"gate:{run_id}" if kind == "gate" and isinstance(run_id, str) and run_id else None
        return [
            Notice(
                user_id,
                kind,
                str(channel.id),
                None,
                title,
                excerpt(body)
                if isinstance(body, str) and body
                else "The details are in the conversation.",
                dedupe,
            )
            for user_id in recipients
        ]

    def _gate(self, session: Any, event: Event) -> list[Notice]:
        channel = self._channel(session, event.channel_id)
        if event.channel_id and channel is None:
            return []
        members = self._viewers(session, channel) if channel is not None else self._members(session)
        item = session.get(WorkItemRow, event.item_id) if event.item_id else None
        what = str(item.title) if item is not None and item.title else ""
        body = (
            f"{excerpt(what)} is waiting for your decision."
            if what
            else "Work is waiting for your decision."
        )
        dedupe = f"gate:{run_public_id(event.run_id)}" if event.run_id else None
        return [
            Notice(
                member.user.id,
                "gate",
                event.channel_id,
                None,
                "Decision needed",
                body,
                dedupe,
            )
            for member in members
            if _can_decide(member)
        ]

    # -- planning (#2349) -----------------------------------------------------------

    def _person(self, session: Any, who: str | None) -> str | None:
        """The active member ``who`` names — a user id, or the id of the
        client a person signs in with (what a principal carries)."""
        if not who:
            return None
        found = self._members(session)
        for member in found:
            if member.user.id == who or member.user.client_id == who:
                return member.user.id
        return None

    def _plan_item(self, session: Any, event: Event) -> WorkItemRow | None:
        """The ``plan`` run's work item: the event's own, else the one its
        run was dispatched for."""
        if event.item_id:
            row = session.get(WorkItemRow, event.item_id)
            if row is not None:
                return row  # type: ignore[no-any-return]
        runs = {str(event.run_id or "")}
        public = str(event.data.get("run_id") or "")
        runs |= {public, parse_run_id(public) or ""}
        runs.discard("")
        if not runs:
            return None
        return session.scalars(  # type: ignore[no-any-return]
            select(WorkItemRow).where(WorkItemRow.run_id.in_(sorted(runs))).limit(1)
        ).first()

    def _breakdown_asker(self, session: Any, event: Event) -> str | None:
        """The person who asked for the breakdown: the author of the chat
        message behind the ``plan`` item, else the person whose operation
        admitted it, else the requester it names. Nobody when none of these
        is an active member: a planning notice is never broadcast."""
        item = self._plan_item(session, event)
        if item is None:
            return None
        if item.message_id:
            message = session.get(MessageRow, item.message_id)
            if message is not None:
                author = _message(session, message).author
                if author.kind == "human":
                    return self._person(session, author.id)
        admitted = session.scalars(
            select(OperationRow)
            .where(
                OperationRow.action == "item.admit",
                OperationRow.target_kind == "item",
                OperationRow.target_key == item.item_id,
            )
            .order_by(OperationRow.accepted_at.asc())
            .limit(1)
        ).first()
        if admitted is not None:
            actor = json.loads(admitted.actor_json or "{}")
            person = self._person(session, str(actor.get("id") or "") or None)
            if person is not None:
                return person
        return self._person(session, item.requested_by)

    @staticmethod
    def _node_title(session: Any, node_id: Any) -> str:
        row = session.get(PlanNodeRow, str(node_id or "")) if node_id else None
        return " ".join(str(row.title).split()) if row is not None and row.title else ""

    def _visible_channel(self, session: Any, channel_id: str | None, user_id: str) -> str | None:
        """``channel_id`` when ``user_id`` can open it, else none."""
        channel = self._channel(session, channel_id)
        if channel is None:
            return None
        if all(member.user.id != user_id for member in self._viewers(session, channel)):
            return None
        return str(channel.id)

    def _breakdown(self, session: Any, event: Event) -> list[Notice]:
        asker = self._breakdown_asker(session, event)
        if asker is None:
            return []
        data = event.data
        node = session.get(PlanNodeRow, str(data.get("node_id") or ""))
        what = " ".join(str(node.title).split()) if node is not None and node.title else ""
        what = what or "your plan"
        child = {"initiative": "epic", "epic": "task"}.get(
            str(node.level) if node is not None else "", "item"
        )
        channel = self._visible_channel(session, event.channel_id, asker)
        if event.type == PLAN_QUESTIONS:
            questions = data.get("questions")
            asked = len(questions) if isinstance(questions, list) else 0
            many = "a question" if asked == 1 else f"{asked} questions" if asked else "questions"
            return [
                Notice(
                    asker,
                    "gate",
                    channel,
                    None,
                    "Questions waiting for you",
                    excerpt(
                        f"The planner has {many} about {what} before it proposes its {child}s."
                    ),
                )
            ]
        count = data.get("count")
        proposed = (
            f"{count} {child if count == 1 else child + 's'}"
            if isinstance(count, int) and count > 0
            else f"the {child}s"
        )
        return [
            Notice(
                asker,
                "work",
                channel,
                None,
                "A proposal is ready for you",
                excerpt(f"The planner proposed {proposed} for {what}. Review and approve it."),
            )
        ]

    def _paused(self, session: Any, event: Event) -> list[Notice]:
        data = event.data
        row = session.get(PlanEpicRunRow, str(data.get("epic_run_id") or ""))
        if row is None:
            return []
        starter = self._person(session, row.started_by)
        if starter is None:
            return []
        epic = self._node_title(session, row.node_id) or "your epic"
        if data.get("reason") == "task_failed":
            task = self._node_title(session, data.get("task_node_id")) or "a task"
            error = str(data.get("error") or "").strip().rstrip(".")
            body = f"{task} failed in {epic}" + (f": {error}." if error else ".")
            body += " Retry or skip it; what depends on it waits."
            title = "Your epic run needs you"
        elif data.get("reason") == "person":
            if self._person(session, str(event.actor.get("id") or "") or None) == starter:
                return []  # your own pause is not news to you
            who = str(data.get("by") or "Someone")
            body = f"{who} paused {epic}. Nothing new starts until it is resumed."
            title = "Your epic run was paused"
        else:
            return []
        return [
            Notice(
                starter,
                "failure",
                None,
                None,
                title,
                excerpt(body),
            )
        ]


__all__ = [
    "TYPES",
    "Event",
    "Notice",
    "NoticeRules",
    "allowed",
    "excerpt",
    "mentions",
]
