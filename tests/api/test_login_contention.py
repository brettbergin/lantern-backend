"""Signing in while the daemon works.

A sign-in checks a password (scrypt, slow on purpose) and records the
client's last use. The daemon meanwhile works: triage retries a transient
failure under the operator's default grant, and the retried run records
its events through the engine's store — a second connection to the same
file. Triage's own writes share the API's connection and its lock, so they
queue behind a sign-in; it is the run it starts that commits beside one.
Here both happen at once, on a clock that moves as a real server's does:
the fixture's frozen clock made ``last_used_at`` a no-op write and hid
this for as long as it stood still.
"""

from __future__ import annotations

import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from lantern.api import errors
from lantern.config import Config
from lantern.engine.model import RunResult
from lantern_worker.protocol import Event
from tests.api.conftest import Api, build
from tests.unit.test_triage import _fail

LOCAL = {
    "email": "alice@example.test",
    "username": "alice-local",
    "password": "correct horse battery staple",
}
SIGN_INS = 24


@pytest.fixture
def served(tmp_path: Path) -> Any:
    built = build(
        tmp_path,
        config={
            "daemon": {"max_attempts_per_item": 1, "max_consecutive_failures": 50},
            "landing": {"retry_rounds": 0},
        },
    )
    with built.client:
        yield built
    built.ctx.close()


def test_sign_ins_succeed_while_a_run_triage_retried_is_in_flight(
    served: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Field failure: ``POST /v1/auth/local/login`` answered 500
    ``internal_error`` to one of many clients signing in at once. Server
    side it was ``sqlite3.OperationalError: database is locked`` on
    ``UPDATE api_clients SET last_used_at``: the row had been read in a
    deferred transaction, a run committed an event on the engine's
    connection while the password was being checked, and the stale snapshot
    could not be upgraded to write."""
    api = served
    assert api.client.post("/v1/auth/local/register", json=LOCAL).status_code == 201

    crashes: list[str] = []
    report = errors.log.error

    def record(*args: Any, **kwargs: Any) -> Any:
        crashes.append(repr(sys.exc_info()[1]))
        return report(*args, **kwargs)

    monkeypatch.setattr(errors.log, "error", record)

    # An issue whose run timed out in CI: what the default grant lets the
    # operator retry, once, with nobody asked.
    item_id = _fail(api.harness)
    in_flight, release = threading.Event(), threading.Event()
    recorded: list[int] = []
    broken: list[str] = []
    scripted = api.loop._runner

    def runner(item: Any, cfg: Config, run_id: str, bus: Any, resume: bool) -> RunResult:
        """The retried run: it records events for as long as it is in
        flight, as a run's agent does with every message and tool call."""
        result: RunResult = scripted(item, cfg, run_id, bus, resume)
        in_flight.set()
        while not release.wait(0.002):
            api.harness.store.append_event(
                Event(ts=api.clock(), run_id=run_id, type="worker.stdout", data={"line": "…"})
            )
            recorded.append(len(recorded))
        return result

    api.loop._runner = runner

    def daemon() -> None:
        api.clock.t += 5
        try:
            api.loop.tick()
        except Exception:
            broken.append(traceback.format_exc())
            in_flight.set()

    def sign_in(_: int) -> int:
        api.clock.t += 1
        return api.client.post(
            "/v1/auth/local/login",
            json={"username": LOCAL["username"], "password": LOCAL["password"]},
        ).status_code

    api.harness.outcomes = ["merged"]
    thread = threading.Thread(target=daemon, name="daemon-tick")
    thread.start()
    try:
        assert in_flight.wait(timeout=30) and not broken, broken
        before = len(recorded)
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(sign_in, range(SIGN_INS)))
        during = len(recorded) - before
    finally:
        release.set()
        thread.join()

    assert crashes == []
    assert statuses == [200] * SIGN_INS
    assert broken == []
    # The run really was recording beside the sign-ins, and it was triage,
    # acting as the operator, that put it in flight.
    assert during > 0
    (decision,) = api.loop.delegation.page(limit=100)
    assert (decision.action, decision.outcome) == ("item.retry", "allow")
    assert api.harness.dstore.get(item_id).state == "done"  # type: ignore[union-attr]
