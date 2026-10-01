"""Workspace sanity: both distributions import and stay in version lockstep."""

import lantern
import lantern_worker


def test_versions_are_lockstep() -> None:
    assert lantern.__version__ == lantern_worker.__version__


def test_version_shape() -> None:
    # hatch-vcs derives the version from git tags: exactly X.Y.Z on a tagged
    # release commit, X.Y.Z.devN on commits in between.
    parts = lantern.__version__.split(".")
    assert len(parts) in (3, 4)
    assert all(p.isdigit() for p in parts[:3])
    if len(parts) == 4:
        assert parts[3].startswith("dev")
