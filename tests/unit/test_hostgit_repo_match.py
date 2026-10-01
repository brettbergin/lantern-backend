"""Unit tests for hostgit.normalise_repo_url / origin_matches_repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from lantern import hostgit

from .test_hostgit import git, make_repo


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:brettbergin/lantern-backend.git",
        "git@github.com:brettbergin/lantern-backend",
        "https://github.com/brettbergin/lantern-backend",
        "https://github.com/brettbergin/lantern-backend.git",
        "https://github.com/brettbergin/lantern-backend/",
        "https://github.com/brettbergin/lantern-backend.git/",
        "https://x-access-token:secret@github.com/brettbergin/lantern-backend.git",
        "git://github.com/brettbergin/lantern-backend.git",
        "ssh://git@github.com/brettbergin/lantern-backend.git",
        "https://GitHub.com/BrettBergin/lantern-backend.git",
        "brettbergin/lantern-backend",
    ],
)
def test_normalise_repo_url_forms(url: str) -> None:
    assert hostgit.normalise_repo_url(url) == "brettbergin/lantern-backend"


def test_normalise_repo_url_case_insensitive() -> None:
    assert hostgit.normalise_repo_url(
        "git@github.com:BrettBergin/lantern-backend.git"
    ) == hostgit.normalise_repo_url("https://github.com/brettbergin/lantern-backend")


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example/acme/platform/widgets.git",
        "git@gitlab.example:acme/platform/widgets.git",
        "acme/platform/widgets",
    ],
)
def test_subgroup_origins_keep_the_full_namespace(url: str) -> None:
    assert hostgit.normalise_repo_url(url) == "acme/platform/widgets"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "https://github.com/",
        "https://github.com/owner",
        "not a url",
        "/tmp/local/repo",
        "C:/code/local/repo",
        "acme//widgets",
        "acme/../widgets",
    ],
)
def test_normalise_repo_url_rejects(url: str | None) -> None:
    assert hostgit.normalise_repo_url(url) is None


def test_origin_matches_repo_true(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    git("remote", "add", "origin", "git@github.com:brettbergin/lantern-backend.git", cwd=root)
    assert hostgit.origin_matches_repo(root, "brettbergin/lantern-backend") is True
    assert (
        hostgit.origin_matches_repo(root, "https://github.com/BrettBergin/lantern-backend.git")
        is True
    )


def test_origin_matches_repo_false_on_mismatch(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    git("remote", "add", "origin", "https://github.com/brettbergin/lantern-backend", cwd=root)
    assert hostgit.origin_matches_repo(root, "brettbergin/entrygraph") is False


def test_origin_matches_repo_none_without_origin(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    assert hostgit.origin_matches_repo(root, "brettbergin/lantern-backend") is None


def test_origin_matches_repo_none_for_non_git_path(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert hostgit.origin_matches_repo(plain, "brettbergin/lantern-backend") is None
    assert hostgit.origin_matches_repo(tmp_path / "missing", "a/b") is None


def test_origin_matches_repo_none_for_unparsable_expectation(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    git("remote", "add", "origin", "https://github.com/brettbergin/lantern-backend", cwd=root)
    assert hostgit.origin_matches_repo(root, "nonsense") is None
