"""Who is addressed when a run ends somewhere only a person can move it.

``run.blocked``, ``run.abandoned`` and the handed-over ``run.exhausted``
say "a human needs to look" — and name the humans: whoever asked for the
work and whoever watches the run, the same list ``run.awaiting_answers``
addresses. A notice the loop will act on by itself (a failed attempt it
retries, the first exhaustion it resumes) addresses nobody, and neither
does the record of a person's own decision (``item.abandoned``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lantern.config import Config
from lantern.daemon.model import DaemonNotice, RunReport, WorkItem
from lantern.engine.model import RunResult
from lantern.events import EventBus
from tests.unit.test_daemon_loop import Harness, RecordingFrontend, gh_item

REQUESTER = "U1"
WATCHER = "U2"


class DrainingFrontend(RecordingFrontend):
    """A frontend whose finish path clears the run's watch registry, as a
    chat bridge's does once it has told the watchers how the run ended."""

    def __init__(self, h: Harness, backend: str) -> None:
        super().__init__()
        self._h, self._backend = h, backend

    def run_finished(self, item: WorkItem, report: RunReport) -> None:
        super().run_finished(item, report)
        self._h.dstore.take_run_watchers(report.run_id, self._backend)


def harness(tmp_path: Path, **overrides: Any) -> tuple[Harness, RecordingFrontend]:
    cfg = Config.model_validate(
        {"home": str(tmp_path / "state"), "github": {"repo": "o/r"}, **overrides}
    )
    h = Harness(tmp_path, cfg)
    front = RecordingFrontend()
    h.loop.frontend = front  # type: ignore[assignment]
    return h, front


def watched(h: Harness, *watchers: str, backend: str = "slack") -> None:
    """Register ``watchers`` on every run the harness starts — a watch is
    keyed by run id, which exists only once the run is dispatched."""

    def runner(item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool) -> RunResult:
        for who in watchers:
            h.dstore.add_run_watch(run_id, who, h.clock(), backend=backend)
        return h.runner(item, cfg, run_id, bus, resume)

    h.loop._runner = runner


def only(front: RecordingFrontend, kind: str) -> DaemonNotice:
    (notice,) = [n for n in front.notices if n.kind == kind]
    return notice


class TestBlocked:
    def test_the_requester_and_the_watchers_are_addressed_once_each(self, tmp_path: Path) -> None:
        h, front = harness(tmp_path)
        # The requester also watches the run: a chat bridge registers
        # whoever asked as a watcher at run start.
        watched(h, REQUESTER, WATCHER)
        h.source.items = [gh_item(requested_by=REQUESTER)]
        h.outcomes = ["blocked"]
        assert h.loop.tick().outcome == "blocked"
        notice = only(front, "run.blocked")
        assert notice.mention_ids == (REQUESTER, WATCHER)
        assert notice.level == "error" and "a human needs to look" in notice.text

    def test_an_item_nobody_asked_for_in_chat_addresses_nobody(self, tmp_path: Path) -> None:
        # A labelled issue, an API admission, a schedule: no requester id,
        # so the notice is the same line it always was.
        h, front = harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["blocked"]
        assert h.loop.tick().outcome == "blocked"
        assert only(front, "run.blocked").mention_ids == ()

    def test_the_watchers_are_read_before_the_finish_path_clears_them(self, tmp_path: Path) -> None:
        h, _front = harness(tmp_path)
        front = DrainingFrontend(h, "slack")
        h.loop.frontend = front  # type: ignore[assignment]
        watched(h, WATCHER)
        h.source.items = [gh_item(requested_by=REQUESTER)]
        h.outcomes = ["blocked"]
        assert h.loop.tick().outcome == "blocked"
        assert only(front, "run.blocked").mention_ids == (REQUESTER, WATCHER)

    def test_a_run_that_ended_completed_without_landing_is_addressed_too(
        self, tmp_path: Path
    ) -> None:
        h, front = harness(tmp_path)
        h.source.items = [gh_item(requested_by=REQUESTER)]
        h.outcomes = ["completed"]
        assert h.loop.tick().outcome == "blocked"
        assert only(front, "run.blocked").mention_ids == (REQUESTER,)


class TestAbandoned:
    def test_the_last_attempt_addresses_the_requester_and_the_watchers(
        self, tmp_path: Path
    ) -> None:
        h, front = harness(tmp_path, daemon={"max_attempts_per_item": 1})
        watched(h, WATCHER)
        h.source.items = [gh_item(requested_by=REQUESTER)]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "failed"
        notice = only(front, "run.abandoned")
        assert notice.mention_ids == (REQUESTER, WATCHER)
        assert notice.level == "error" and "abandoned after 1 attempt(s)" in notice.text

    def test_a_failure_the_loop_retries_addresses_nobody(self, tmp_path: Path) -> None:
        # Nobody has to act: the item is back in the queue.
        h, front = harness(tmp_path, daemon={"max_attempts_per_item": 2})
        watched(h, WATCHER)
        h.source.items = [gh_item(requested_by=REQUESTER)]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "retry"
        assert only(front, "run.failed").mention_ids == ()
        assert not [n for n in front.notices if n.kind == "run.abandoned"]


class TestExhausted:
    def test_the_automatic_grant_addresses_nobody(self, tmp_path: Path) -> None:
        # The same run resumes with more rounds after the backoff: there is
        # nothing for a person to do.
        h, front = harness(tmp_path, daemon={"retry_backoff_s": 100})
        watched(h, WATCHER)
        h.source.items = [gh_item(requested_by=REQUESTER)]
        h.outcomes = ["exhausted"]
        assert h.loop.tick().outcome == "retry"
        notice = only(front, "run.exhausted")
        assert "resuming the same run" in notice.text and notice.level == "warning"
        assert notice.mention_ids == ()

    def test_the_hand_over_addresses_the_requester_and_the_watchers(self, tmp_path: Path) -> None:
        h, front = harness(tmp_path, daemon={"retry_backoff_s": 1})
        watched(h, WATCHER)
        h.source.items = [gh_item(requested_by=REQUESTER)]
        h.outcomes = ["exhausted", "exhausted"]
        assert h.loop.tick().outcome == "retry"
        h.clock.t += 2
        assert h.loop.tick().outcome == "failed"
        granted, handed_over = [n for n in front.notices if n.kind == "run.exhausted"]
        assert granted.mention_ids == ()
        assert "handed over" in handed_over.text and handed_over.level == "error"
        assert handed_over.mention_ids == (REQUESTER, WATCHER)

    def test_a_hand_over_with_no_rounds_to_grant_is_addressed_at_once(self, tmp_path: Path) -> None:
        h, front = harness(tmp_path, landing={"retry_rounds": 0})
        watched(h, WATCHER)
        h.source.items = [gh_item(requested_by=REQUESTER)]
        h.outcomes = ["exhausted"]
        assert h.loop.tick().outcome == "failed"
        notice = only(front, "run.exhausted")
        assert "handed over" in notice.text
        assert notice.mention_ids == (REQUESTER, WATCHER)


class TestOperatorAbandon:
    def test_a_persons_own_abandon_addresses_nobody(self, tmp_path: Path) -> None:
        # `item.abandoned` records a decision somebody just took — like
        # `run.cancelled`, it is not a run waiting for a person.
        h, front = harness(tmp_path)
        h.dstore.upsert_new(gh_item(requested_by=REQUESTER), now=1.0)
        h.loop.abandon_item("gh:issue:1")
        notice = only(front, "item.abandoned")
        assert "abandoned by operator" in notice.text
        assert notice.mention_ids == ()


class TestAwaitingAnswers:
    def test_the_watchers_are_read_before_the_finish_path_clears_them(self, tmp_path: Path) -> None:
        h, _front = harness(tmp_path)
        front = DrainingFrontend(h, "slack")
        h.loop.frontend = front  # type: ignore[assignment]
        watched(h, WATCHER)
        h.source.items = [gh_item(requested_by=REQUESTER)]
        h.outcomes = ["awaiting_answers"]
        assert h.loop.tick().outcome == "awaiting_answers"
        assert only(front, "run.awaiting_answers").mention_ids == (REQUESTER, WATCHER)
