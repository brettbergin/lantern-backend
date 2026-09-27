"""The byte-identity gate for ``plan`` runs.

A plan run's promise is narrow: one sandbox, a checkout the host cut, one
planner turn (two when the first answer is sent back), and a delivery to the
plan record — never a github sandbox, never a forge write. This test drives
the canonical plan scripts (a proposal delivered, a proposal invalid twice)
and compares the ordered trail each leaves against
``tests/fixtures/plan_run_trail/<scenario>.json``, the way
``test_code_run_trail.py`` holds a code run and ``test_tool_run_trail.py`` a
tool run.

The fixture is a recording, not a derivation: regenerate it only on
purpose, with ``pytest --update-trail``, and read the diff before you
commit it. A change here is either a bug in the PR or a deliberate change
to what a plan run does, and the review should know which."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sbxloop import hostgit
from tests.conftest import FakeSbx
from tests.fakes.gitrepo import make_repo
from tests.unit.test_code_run_trail import trail
from tests.unit.test_engine import Harness
from tests.unit.test_engine_plan import REPO, RecordingDesk, answer, code_task, engine

FIXTURES = Path(__file__).parent.parent / "fixtures" / "plan_run_trail"


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    origin = make_repo(tmp_path, "upstream", {"README.md": "# app\n"})
    monkeypatch.setattr(
        hostgit,
        "clone_from_remote",
        lambda url, target, branch, **kw: hostgit.clone_for_run(origin, target, branch),
    )
    return Harness(fake_sbx, tmp_path, monkeypatch)


def scenario_proposal_delivered(harness: Harness) -> str:
    harness.script([answer(code_task("c1"), code_task("c2", deps=["c1"]))])
    result = engine(harness, RecordingDesk()).start("plan", repo=REPO, kind="plan")
    assert result.state == "completed", result.reason
    return result.run_id


def scenario_invalid_twice(harness: Harness) -> str:
    bad = answer(code_task("c1", verify_commands=[]))
    harness.script([bad, bad])
    result = engine(harness, RecordingDesk()).start("plan", repo=REPO, kind="plan")
    assert result.state == "failed"
    return result.run_id


SCENARIOS = {
    "proposal_delivered": scenario_proposal_delivered,
    "invalid_twice": scenario_invalid_twice,
}


def _stable(value: Any, tmp: str) -> Any:
    """The trail with the test's own temporary directory named, not spelled."""
    return json.loads(json.dumps(value).replace(tmp, "<tmp>"))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_plan_run_trail_matches_the_recording(
    harness: Harness, name: str, request: pytest.FixtureRequest
) -> None:
    run_id = SCENARIOS[name](harness)
    actual = _stable(trail(harness, run_id), str(harness.tmp_path.resolve()))
    actual = _stable(actual, str(harness.tmp_path))
    fixture = FIXTURES / f"{name}.json"
    if request.config.getoption("--update-trail"):
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    assert fixture.is_file(), f"{fixture} is missing; record it with --update-trail"
    expected = json.loads(fixture.read_text())
    assert actual == expected, (
        f"the {name} plan run's trail changed; if that is deliberate, re-record "
        "with `pytest tests/unit/test_plan_run_trail.py --update-trail` and review the diff"
    )


def test_no_forge_write_appears_in_any_trail() -> None:
    """Whatever else the fixtures hold, none of them may hold a github
    sandbox, a delivery or a landing."""
    for name in sorted(SCENARIOS):
        recorded = json.loads((FIXTURES / f"{name}.json").read_text())
        types = {entry["type"] for entry in recorded["events"]}
        assert not any(t.startswith(("run.deliver", "land.", "review.", "ci.")) for t in types)
        roles = {entry.get("role") for entry in recorded["sandbox_events"]}
        assert "github" not in roles, (name, roles)
        assert "proposing" in recorded["states"]
