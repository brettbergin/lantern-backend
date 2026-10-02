"""The ``units`` row of ``lantern doctor``: the user units `lantern init
--systemd` rendered and linked, and anything on the host that quietly
replaces what they say."""

from __future__ import annotations

from pathlib import Path

import pytest

from lantern.cli.doctor import Check, _launcher_checks
from lantern.homeinit import UNIT_NAMES
from tests.unit.test_homeinit import make


def _units_row(tmp_path: Path) -> Check:
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
    home = make(tmp_path, systemd=True)[0]
    return next(c for c in _launcher_checks(home, env) if c.name == "units")


@pytest.fixture
def linked_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialised home whose units are enabled the way `systemctl
    --user enable` does it: a symlink per unit under the user's units dir."""
    monkeypatch.setattr(
        "lantern.cli.doctor.shutil.which",
        lambda name, path=None: "/usr/bin/systemctl" if name == "systemctl" else None,
    )
    monkeypatch.setattr("lantern.cli.doctor._service_checks", lambda home, env: [])
    home, init, _run, _fetch, _said = make(tmp_path, systemd=True)
    init.execute()
    user_units = tmp_path / ".config" / "systemd" / "user"
    user_units.mkdir(parents=True)
    for name in UNIT_NAMES:
        (user_units / name).symlink_to(home.unit(name))
    return user_units


def test_rendered_and_linked_units_are_ok(tmp_path: Path, linked_home: Path) -> None:
    row = _units_row(tmp_path)
    assert row.ok and not row.hard
    assert "linked from" in row.detail


def test_a_drop_in_overriding_a_unit_is_named(tmp_path: Path, linked_home: Path) -> None:
    """A hand-written drop-in replaced `ExecStart=` with a binary that no
    longer existed on a production host, and doctor said the units were
    linked: the links *were* fine. Init never removes a drop-in, so the
    remedy has to say so rather than point at `init --systemd` alone."""
    conf = linked_home / "sbx-sandboxd.service.d" / "override.conf"
    conf.parent.mkdir()
    conf.write_text("[Service]\nExecStart=\nExecStart=/nowhere/sbx daemon start -d\n")
    row = _units_row(tmp_path)
    assert not row.ok and not row.hard
    assert f"sbx-sandboxd.service is overridden by {conf}" in row.detail
    assert "drop-in" in row.detail
    assert row.detail.endswith("remove the drop-in(s) or run `lantern init --systemd`")
