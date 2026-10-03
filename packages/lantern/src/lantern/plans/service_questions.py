"""A plan run's clarifying questions on their node: asked, posted, answered or
skipped, withdrawn (#2345).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from lantern.api.publicids import run_public_id
from lantern.engine.planning import (
    Clarification,
    PlanAnswer,
    PlanQuestion,
)
from lantern.log import get_logger
from lantern.plans.model import (
    Plan,
)
from lantern.plans.service_base import (
    PLANNER,
    PlanRefusal,
    _checked_answers,
    _not_found,
    _ServiceBase,
    _stale,
    _waiting,
)
from lantern.plans.store import (
    PlanEvent,
    PlanGone,
    StaleRevision,
    retry_stale,
)

log = get_logger(__name__)


class _Questions(_ServiceBase):
    def ask_questions(
        self,
        plan_id: str,
        node_id: str,
        questions: Sequence[PlanQuestion],
        *,
        run_id: str,
        now: float,
        item_id: str | None = None,
        channel_id: str | None = None,
    ) -> Plan:
        """Put a plan run's clarifying questions on its node for a person to
        answer, replacing any earlier generation's, with
        ``plan.generation.questions`` in the same write. Written against the
        revision it reads, and read again when another write won."""

        def attempt() -> Plan:
            plan, node = self.breakdown_target(plan_id, node_id)
            asked = Clarification(run_id=run_id, questions=list(questions), asked_at=now)
            try:
                return self.store.apply(
                    plan.id,
                    expected_revision=plan.revision,
                    now=now,
                    upsert=[replace(node, generation=asked)],
                    events=[
                        PlanEvent(
                            "plan.generation.questions",
                            {
                                "plan_id": plan.id,
                                "node_id": node.id,
                                "run_id": run_public_id(run_id),
                                "questions": [q.model_dump(mode="json") for q in questions],
                            },
                            run_id=run_id,
                            item_id=item_id,
                            channel_id=channel_id,
                        )
                    ],
                    actor=dict(PLANNER),
                )
            except PlanGone as exc:
                raise _not_found(plan_id) from exc

        try:
            return retry_stale(attempt)
        except StaleRevision as exc:
            raise PlanRefusal(
                409, "stale_revision", "the plan kept changing while the questions were written"
            ) from exc

    def record_question_posts(
        self,
        plan_id: str,
        node_id: str,
        *,
        run_id: str,
        posts: Mapping[str, str],
        now: float,
    ) -> None:
        """Remember where a chat bridge posted the questions ``run_id`` is
        waiting on (``<backend>:<message id>`` → question id), so a reply to
        a post or a click on its buttons finds its question from the plan
        record after a restart. Nothing when the questions are no longer
        waiting or another run asked them; no event — nothing a person
        reads changed."""

        def attempt() -> None:
            plan = self.get(plan_id)
            node = self._node(plan, node_id)
            waiting = node.generation
            if waiting is None or waiting.settled or waiting.run_id != run_id:
                return
            known = {k: q for k, q in posts.items() if waiting.question(q) is not None}
            if not known or all(waiting.posts.get(k) == q for k, q in known.items()):
                return
            remembered = waiting.model_copy(update={"posts": {**waiting.posts, **known}})
            try:
                self.store.apply(
                    plan.id,
                    expected_revision=plan.revision,
                    now=now,
                    upsert=[replace(node, generation=remembered)],
                )
            except PlanGone as exc:
                raise _not_found(plan_id) from exc
            return

        try:
            return retry_stale(attempt)
        except StaleRevision as exc:
            raise PlanRefusal(
                409, "stale_revision", "the plan kept changing while the posts were recorded"
            ) from exc

    def waiting_questions(self, plan_id: str, node_id: str) -> Clarification:
        """The node's clarifying questions while they wait for a person,
        refused by name when nothing waits: none were asked, or they were
        already answered, skipped or withdrawn."""
        node = self._node(self.get(plan_id), node_id)
        return _waiting(node)

    def pending_questions(self, plan_id: str, node_id: str) -> Clarification | None:
        """:meth:`waiting_questions`, or None when nothing is waiting."""
        try:
            return self.waiting_questions(plan_id, node_id)
        except PlanRefusal:
            return None

    def answer_questions(
        self,
        plan_id: str,
        node_id: str,
        *,
        answers: Mapping[str, PlanAnswer],
        skip: bool,
        settle: bool,
        now: float,
        actor: Mapping[str, Any],
        expected_revision: int | None = None,
        run_id: str | None = None,
        item_id: str | None = None,
        channel_id: str | None = None,
    ) -> tuple[Plan, Clarification]:
        """Record a person's answers to the node's waiting questions — each
        a choice's ``value`` or, where the question allows it, their own
        ``text`` — or their ``skip``. Answers already given (one question at
        a time from chat) are kept; a later answer to the same question
        replaces it. The questions are settled when ``skip`` or ``settle``
        says so (the API's submit) or when every one has an answer, and
        then ``plan.generation.answered`` is written in the same revision.

        ``run_id``, when given, is the run the caller is answering: an
        answer meant for questions another run has since replaced is
        refused rather than recorded against them."""

        def attempt() -> tuple[Plan, Clarification]:
            plan = self.get(plan_id)
            if expected_revision is not None:
                self._check_revision(plan, expected_revision)
            self._not_archived(plan)
            node = self._node(plan, node_id)
            waiting = _waiting(node)
            if run_id is not None and waiting.run_id != run_id:
                raise PlanRefusal(
                    409,
                    "no_questions",
                    "those questions were replaced by a later breakdown",
                    node_id=node.id,
                )
            if skip and answers:
                raise PlanRefusal(
                    422, "invalid_argument", "answer the questions or skip them, not both"
                )
            recorded = {**waiting.answers, **_checked_answers(waiting, answers)}
            if settle and not skip and not recorded:
                raise PlanRefusal(
                    422, "invalid_argument", "answer at least one question, or skip them"
                )
            done = skip or settle or all(q.id in recorded for q in waiting.questions)
            status = "skipped" if skip else "answered" if done else "awaiting_answers"
            who = str(actor.get("display") or actor.get("id") or "someone")
            updated = waiting.model_copy(
                update={
                    "answers": recorded,
                    "status": status,
                    "answered_at": now if done else waiting.answered_at,
                    "answered_by": who,
                }
            )
            data: dict[str, Any] = {
                "plan_id": plan.id,
                "node_id": node.id,
                "run_id": run_public_id(waiting.run_id),
            }
            if done:
                event = PlanEvent(
                    "plan.generation.answered",
                    {
                        **data,
                        "skipped": skip,
                        "answers": {
                            qid: answer.model_dump(mode="json", exclude_defaults=True)
                            for qid, answer in recorded.items()
                        },
                    },
                    run_id=waiting.run_id,
                    item_id=item_id,
                    channel_id=channel_id,
                )
            else:
                event = PlanEvent(
                    "plan.node.changed",
                    {**data, "change": "answered"},
                    run_id=waiting.run_id,
                    item_id=item_id,
                    channel_id=channel_id,
                )
            try:
                changed = self.store.apply(
                    plan.id,
                    expected_revision=plan.revision,
                    now=now,
                    upsert=[replace(node, generation=updated)],
                    events=[event],
                    actor=dict(actor),
                )
            except StaleRevision as exc:
                if expected_revision is not None:
                    raise _stale(exc) from exc
                raise
            except PlanGone as exc:
                raise _not_found(plan_id) from exc
            return changed, updated

        try:
            return retry_stale(attempt)
        except StaleRevision as exc:
            raise PlanRefusal(
                409, "stale_revision", "the plan kept changing while the answers were written"
            ) from exc

    def withdraw_questions(
        self,
        plan_id: str,
        node_id: str,
        *,
        run_id: str,
        reason: str,
        now: float,
        item_id: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        """The run waiting on the node's questions was given up before a
        person answered: the questions are withdrawn, so no client keeps
        offering them, and the generation ends ``plan.generation.failed``.
        Nothing when the questions are another run's or already settled."""

        def attempt() -> None:
            plan = self.store.get(plan_id)
            node = plan.node(node_id) if plan is not None else None
            if plan is None or node is None:
                return
            waiting = node.generation
            if waiting is None or waiting.run_id != run_id or waiting.status != "awaiting_answers":
                return
            try:
                self.store.apply(
                    plan.id,
                    expected_revision=plan.revision,
                    now=now,
                    upsert=[
                        replace(node, generation=waiting.model_copy(update={"status": "withdrawn"}))
                    ],
                    events=[
                        PlanEvent(
                            "plan.generation.failed",
                            {
                                "plan_id": plan.id,
                                "node_id": node.id,
                                "run_id": run_public_id(run_id),
                                "reason": reason,
                            },
                            run_id=run_id,
                            item_id=item_id,
                            channel_id=channel_id,
                        )
                    ],
                    actor=dict(PLANNER),
                )
            except PlanGone:
                return
            return

        try:
            return retry_stale(attempt)
        except StaleRevision:
            return None
