"""The plan store's one shape for a write against the plan as it is now."""

from __future__ import annotations

import pytest

from lantern.plans.store import StaleRevision, retry_stale


def test_a_write_that_loses_to_another_is_tried_again() -> None:
    calls = 0

    def attempt() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise StaleRevision(calls)
        return "written"

    assert retry_stale(attempt) == "written"
    assert calls == 3


def test_a_write_that_keeps_losing_tells_the_caller_the_last_revision() -> None:
    calls = 0

    def attempt() -> None:
        nonlocal calls
        calls += 1
        raise StaleRevision(10 + calls)

    with pytest.raises(StaleRevision) as caught:
        retry_stale(attempt)
    assert (calls, caught.value.current) == (3, 13)


def test_anything_else_the_attempt_raises_is_not_retried() -> None:
    calls = 0

    def attempt() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("not a lost race")

    with pytest.raises(ValueError):
        retry_stale(attempt)
    assert calls == 1
