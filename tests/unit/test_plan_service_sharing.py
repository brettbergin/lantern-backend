"""One plan service per daemon.

The service's mutual exclusion — a plan being published, reconciled or
written to the forge is not published, reconciled or written again until
that finishes — lives in sets on the instance. It only holds when every
surface in the process (the API, intake, the concierge, the loop's own
settle and launch paths) shares the one instance the daemon owns.
"""

from __future__ import annotations

import re
from pathlib import Path

import lantern
from lantern.api.auth.keys import load_or_create
from lantern.api.auth.store import ApiAuthStore
from lantern.api.context import ApiContext
from lantern.config import Config
from lantern.daemon.controls.intake import PlanAdmission, plan_item
from lantern.ghids import api_item_id
from lantern.plans import service as plan_service
from tests.unit.test_daemon_loop import Harness

PERSON = {"kind": "person", "id": "p1", "display": "Pat"}


def test_the_daemon_owns_one_plan_service_every_surface_shares(tmp_path: Path, monkeypatch) -> None:
    config = Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "api": {"enabled": True},
        }
    )
    harness = Harness(tmp_path, config)
    loop = harness.loop
    service = loop.plans
    assert loop.plans is service, "the loop rebuilt its plan service"

    ctx = ApiContext(
        config,
        loop=loop,
        auth=ApiAuthStore(harness.dstore),
        keys=load_or_create(config.paths),
        clock=harness.clock,
    )
    assert ctx.plans is service, "the API built its own plan service"

    # Intake admits a breakdown against the same instance: a service built
    # per admission would be blind to a publish the API holds.
    built: list[object] = []
    original = plan_service.PlanService.__init__

    def counting(self: object, *args: object, **kwargs: object) -> None:
        built.append(self)
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(plan_service.PlanService, "__init__", counting)
    plan = service.create(
        level="epic",
        repository="o/r",
        sections={"title": "Export reports", "goal": "download reports as CSV"},
        now=1.0,
        actor=PERSON,
    )
    plan_item(
        loop,
        PlanAdmission(plan.id, plan.root_id, expected_revision=plan.revision),
        item_id=api_item_id("plan:k1"),
    )
    assert built == [], "intake built its own plan service"


def test_the_plan_service_is_built_in_one_place() -> None:
    """A second construction site is a second set of busy plans."""
    root = Path(lantern.__file__).parent
    sites = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if re.search(r"\bPlanService\(", path.read_text())
    )
    assert sites == ["daemon/loop.py"], sites
