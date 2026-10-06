"""Fleet analytics over HTTP: the console's fold of a window of runs —
outcomes, time to land, time parked, turns, failures by cause — flattened
so a client reads every derived value as a field, compared with the window
before it, and never a currency."""

from __future__ import annotations

import time
from typing import Any

from lantern_worker.protocol import Usage
from tests.api.conftest import Api
from tests.fakes.rawdb import exec_raw
from tests.unit.test_daemon_loop import gh_item

DAY = 86400.0


def _run(api: Api, key: str, outcome: str = "merged") -> str:
    api.harness.source.items = [gh_item(key)]
    api.harness.outcomes = [outcome]
    api.clock.t += 10
    api.loop.tick()
    return api.harness.runs[-1][0]


def _shape(
    api: Api,
    run_id: str,
    *,
    created: float,
    elapsed: float,
    active: float,
    turns: int,
    reason: str | None = None,
) -> None:
    """Give a run the clock and the cost a live one would have: when it
    began, how long it took end to end, and one phase attempt that ran for
    ``active`` of that."""
    store = api.harness.store
    exec_raw(
        store,
        "UPDATE runs SET created_at=?, updated_at=?, reason=? WHERE run_id=?",
        (created, created + elapsed, reason, run_id),
    )
    store.record_phase(
        run_id,
        "build",
        task_id="t1",
        attempt=1,
        status="ok",
        output_json="{}",
        started_at=created,
        turns=turns,
        usage=Usage(input_tokens=turns * 1000, output_tokens=0, cache_read_tokens=turns * 5000),
    )
    exec_raw(
        store, "UPDATE phase_attempts SET ended_at=? WHERE run_id=?", (created + active, run_id)
    )


def _window(api: Api, **params: Any) -> dict[str, Any]:
    # Run records carry the wall clock, as they do in a live daemon; the
    # harness clock reads 1970, so a window that holds them names its end.
    params.setdefault("until", time.time() + 60)
    response = api.client.get("/v1/analytics", params=params, headers=api.bearer())
    assert response.status_code == 200, response.text
    return dict(response.json())


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for inner in value.values() for key in _keys(inner)}
    if isinstance(value, list):
        return {key for inner in value for key in _keys(inner)}
    return set()


class TestTheWindow:
    def test_an_empty_window_says_so_and_still_has_every_bucket(self, api: Api) -> None:
        # No `until`: the window ends at the daemon's own clock, a week wide,
        # a day per bucket.
        response = api.client.get("/v1/analytics", headers=api.bearer())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["empty"] is True and body["workspace_id"] == "local"
        assert body["window_s"] == 7 * 86400
        assert body["since"].startswith("1970-01-05") and body["until"].startswith("1970-01-12")
        assert body["observed_at"] == body["until"]
        assert body["total"] == {
            "kind": "all",
            "runs": 0,
            "landed": 0,
            "failed": 0,
            "cancelled": 0,
            "turns": 0,
            "tokens": 0,
            "cache_read_tokens": 0,
            "active_s": 0.0,
            "elapsed_s": 0.0,
            "parked_s": 0.0,
            "ok_rate": None,
            "parked_share": 0.0,
        }
        assert body["lanes"] == [] and body["phases"] == [] and body["failures"] == []
        assert body["costliest"] == [] and body["longest_parked"] == []
        assert len(body["buckets"]) == 7
        assert all(
            (b["runs"], b["landed"], b["failed"], b["cancelled"], b["turns"]) == (0, 0, 0, 0, 0)
            for b in body["buckets"]
        )
        # The buckets tile the window: each starts where the last one ended.
        assert body["buckets"][0]["since"] == body["since"]
        assert body["buckets"][-1]["until"] == body["until"]
        assert all(
            a["until"] == b["since"]
            for a, b in zip(body["buckets"], body["buckets"][1:], strict=False)
        )
        # Nothing ran, so there is no middle to report and nothing to
        # compare with: unknown, not zero.
        assert body["spreads"] == {"turns": None, "cycle_s": None, "active_s": None}
        assert body["previous"] is None
        assert set(body["delta"].values()) == {None}
        assert body["rework"] == {
            "tasks": 0,
            "revisions": 0,
            "replans": 0,
            "suspect": 0,
            "retried_share": 0.0,
        }
        assert body["review_rounds"] == 0 and body["ci_rounds"] == 0

    def test_landed_and_failed_runs_fold_into_lanes_buckets_and_causes(self, api: Api) -> None:
        now = time.time()
        landed = _run(api, "1")
        failed = _run(api, "2", "failed")
        api.harness.store.bump_run_counter(landed, "review_rounds")
        # The landed run worked ten minutes of an hour; the failed one was
        # quick and says why.
        _shape(api, landed, created=now - 7200, elapsed=3600.0, active=600.0, turns=12)
        _shape(
            api,
            failed,
            created=now - 3600,
            elapsed=300.0,
            active=200.0,
            turns=4,
            reason="verify failed: exit status 1 in the third task",
        )
        body = _window(api)
        assert body["empty"] is False
        total = body["total"]
        assert (total["runs"], total["landed"], total["failed"], total["cancelled"]) == (2, 1, 1, 0)
        assert total["turns"] == 16 and total["tokens"] == 16000
        assert total["cache_read_tokens"] == 80000
        assert total["active_s"] == 800.0 and total["elapsed_s"] == 3900.0
        # Derived values are fields: a client never recomputes them.
        assert total["parked_s"] == 3100.0
        assert total["ok_rate"] == 0.5
        assert round(total["parked_share"], 3) == round(3100 / 3900, 3)
        # One kind ran, so its lane is the total under its own name.
        assert body["lanes"] == [{**total, "kind": "code"}]
        assert body["phases"] == [
            {
                "phase": "build",
                "attempts": 2,
                "retries": 0,
                "turns": 16,
                "tokens": 16000,
                "cache_read_tokens": 80000,
                "active_s": 800.0,
            }
        ]
        # Both began in the window's last day.
        *earlier, last = body["buckets"]
        assert (last["runs"], last["landed"], last["failed"], last["turns"]) == (2, 1, 1, 16)
        assert all(b["runs"] == 0 for b in earlier)
        assert last["since"].endswith("Z") and last["until"] == body["until"]
        # A failure is grouped by its cause, not by the run's own detail.
        assert body["failures"] == [{"reason": "verify failed", "count": 1}]
        assert body["review_rounds"] == 1 and body["ci_rounds"] == 0
        # Outliers carry the public run id every other route does.
        assert [r["run_id"] for r in body["costliest"]] == [f"run_{landed}", f"run_{failed}"]
        assert body["costliest"][0] == {
            "run_id": f"run_{landed}",
            "kind": "code",
            "state": "merged",
            "turns": 12,
            "tokens": 12000,
            "active_s": 600.0,
            "parked_s": 3000.0,
        }
        assert body["longest_parked"][0]["run_id"] == f"run_{landed}"
        assert body["longest_parked"][0]["parked_s"] == 3000.0
        assert api.client.get(f"/v1/runs/run_{landed}", headers=api.bearer()).status_code == 200
        # Time to land is taken over the runs that landed, end to end.
        assert body["spreads"]["cycle_s"] == {"median": 3600.0, "p90": 3600.0}
        assert body["spreads"]["turns"] == {"median": 12.0, "p90": 12.0}
        assert body["spreads"]["active_s"] == {"median": 600.0, "p90": 600.0}
        # Telemetry, never an invoice.
        assert not {k for k in _keys(body) if "spend" in k or "usd" in k or "price" in k}

    def test_the_window_and_its_buckets_are_the_callers_to_size(self, api: Api) -> None:
        now = time.time()
        recent = _run(api, "1")
        older = _run(api, "2")
        _shape(api, recent, created=now - 600, elapsed=300.0, active=100.0, turns=3)
        _shape(api, older, created=now - 2 * DAY, elapsed=300.0, active=100.0, turns=5)
        hour = _window(api, window_s=3600, buckets=4)
        assert hour["window_s"] == 3600 and len(hour["buckets"]) == 4
        assert hour["total"]["runs"] == 1 and hour["total"]["turns"] == 3
        month = _window(api, window_s=30 * 86400, buckets=30)
        assert month["total"]["runs"] == 2 and len(month["buckets"]) == 30
        assert sum(b["runs"] for b in month["buckets"]) == 2
        # An RFC 3339 end works as an epoch one does; a window that ended
        # before either run began holds neither.
        before = _window(api, until="2001-01-01T00:00:00Z")
        assert before["empty"] is True and before["until"] == "2001-01-01T00:00:00Z"


class TestThePreviousWindow:
    def test_the_previous_window_is_what_the_delta_is_measured_against(self, api: Api) -> None:
        now = time.time()
        first = _run(api, "1")
        second = _run(api, "2")
        old = _run(api, "3")
        _shape(api, first, created=now - 7200, elapsed=600.0, active=300.0, turns=3)
        _shape(api, second, created=now - 3600, elapsed=600.0, active=300.0, turns=1)
        # Nine days back: outside this week, inside the one before it.
        _shape(api, old, created=now - 9 * DAY, elapsed=1200.0, active=300.0, turns=8)
        body = _window(api)
        assert body["total"]["runs"] == 2
        previous = body["previous"]
        assert previous["kind"] == "previous" and previous["runs"] == 1
        assert previous["turns"] == 8 and previous["parked_s"] == 900.0
        delta = body["delta"]
        assert delta["runs"] == 1.0, "two runs against one: doubled"
        assert delta["turns"] == -0.5, "four turns against eight: halved"
        assert delta["active_s"] == 1.0
        assert round(delta["parked_s"], 3) == round((600 - 900) / 900, 3)
        assert delta["ok_rate"] == 0.0, "every judged run landed in both"
        assert delta["failed"] is None, "a change from nothing is not a percentage"

    def test_nothing_before_the_window_is_null_not_zero(self, api: Api) -> None:
        _run(api, "1")
        body = _window(api)
        assert body["total"]["runs"] == 1 and body["previous"] is None
        assert set(body["delta"].values()) == {None}


class TestTheContract:
    def test_bad_parameters_are_refused(self, api: Api) -> None:
        headers = api.bearer()
        for params in (
            {"window_s": 59},
            {"window_s": 91 * 86400},
            {"window_s": "a-week"},
            {"buckets": 0},
            {"buckets": 91},
            {"until": "yesterday"},
            {"until": "nan"},
            {"until": "1e300"},
            {"until": -1},
            # A week ending an hour into 1970 would begin in 1969.
            {"until": 3600},
            {"until": "3000-01-01T00:00:00Z"},
        ):
            refused = api.client.get("/v1/analytics", params=params, headers=headers)
            assert refused.status_code == 422, (params, refused.text)
            assert refused.json()["code"] == "invalid_request", params

    def test_the_window_needs_runs_read(self, api: Api) -> None:
        headers = api.bearer(frozenset({"collaboration:read"}))
        assert api.client.get("/v1/analytics", headers=headers).status_code == 403
        assert api.client.get("/v1/analytics").status_code == 401

    def test_the_feature_is_advertised(self, api: Api) -> None:
        features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
        assert "analytics" in features
