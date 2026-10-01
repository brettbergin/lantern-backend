"""Origin-mismatch preflight: doctor FAILs and daemon start refuses when an
enabled repository's workspace is a checkout of a *different* repository
(#526 — the field failure where a run for entrygraph was built from an
lantern tree)."""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo
from typer.testing import CliRunner

from lantern.cli.app import app
from lantern.cli.doctor import workspace_origin_checks, workspace_origin_mismatches
from lantern.config import load_config

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def make_checkout(path: Path, repo: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_repo = Repo.init(path)
    git_repo.create_remote("origin", f"https://github.com/{repo}")
    git_repo.close()
    return path


def config_for(workdir: Path, toml: str) -> object:
    (workdir / "lantern.toml").write_text(toml)
    return load_config()


FIELD_CONFIG = """
[sandbox]
workspace = "{workspace}"

[[github.repos]]
repo = "brettbergin/lantern-backend"

[[github.repos]]
repo = "brettbergin/entrygraph"
"""


class TestWorkspaceOriginMismatch:
    def test_field_configuration_is_a_mismatch(self, workdir: Path) -> None:
        ws = make_checkout(workdir / "repo", "brettbergin/lantern-backend")
        config = config_for(workdir, FIELD_CONFIG.format(workspace=ws))
        (mismatch,) = workspace_origin_mismatches(config)  # type: ignore[arg-type]
        assert mismatch.repo == "brettbergin/entrygraph"
        assert mismatch.origin_repo == "brettbergin/lantern-backend"
        assert mismatch.path == ws

    def test_the_message_names_both_repos_the_path_and_the_remedy(self, workdir: Path) -> None:
        ws = make_checkout(workdir / "repo", "brettbergin/lantern-backend")
        config = config_for(workdir, FIELD_CONFIG.format(workspace=ws))
        (row,) = workspace_origin_checks(config)  # type: ignore[arg-type]
        assert not row.ok and row.hard
        assert "brettbergin/entrygraph" in row.detail
        assert "brettbergin/lantern-backend" in row.detail
        assert str(ws) in row.detail
        assert "[[vcs.repos]]" in row.detail  # the remedy names the current spelling (#2255)

    def test_single_repo_legacy_workspace_passes(self, workdir: Path) -> None:
        ws = make_checkout(workdir / "repo", "brettbergin/lantern-backend")
        config = config_for(
            workdir,
            f'[sandbox]\nworkspace = "{ws}"\n\n[[github.repos]]\n'
            'repo = "brettbergin/lantern-backend"\n',
        )
        assert workspace_origin_mismatches(config) == []  # type: ignore[arg-type]

    def test_per_repo_workspaces_pass(self, workdir: Path) -> None:
        a = make_checkout(workdir / "a", "brettbergin/lantern-backend")
        b = make_checkout(workdir / "b", "brettbergin/entrygraph")
        config = config_for(
            workdir,
            f'[[github.repos]]\nrepo = "brettbergin/lantern-backend"\nworkspace = "{a}"\n'
            f'[[github.repos]]\nrepo = "brettbergin/entrygraph"\nworkspace = "{b}"\n',
        )
        assert workspace_origin_mismatches(config) == []  # type: ignore[arg-type]

    def test_a_per_repo_workspace_of_the_wrong_repo_fails(self, workdir: Path) -> None:
        a = make_checkout(workdir / "a", "brettbergin/lantern-backend")
        config = config_for(
            workdir,
            f'[[github.repos]]\nrepo = "brettbergin/entrygraph"\nworkspace = "{a}"\n',
        )
        (mismatch,) = workspace_origin_mismatches(config)  # type: ignore[arg-type]
        assert mismatch.repo == "brettbergin/entrygraph"

    def test_a_disabled_repo_is_not_checked(self, workdir: Path) -> None:
        ws = make_checkout(workdir / "repo", "brettbergin/lantern-backend")
        config = config_for(
            workdir,
            f'[sandbox]\nworkspace = "{ws}"\n\n'
            '[[github.repos]]\nrepo = "brettbergin/lantern-backend"\n'
            '[[github.repos]]\nrepo = "brettbergin/entrygraph"\nenabled = false\n',
        )
        assert workspace_origin_mismatches(config) == []  # type: ignore[arg-type]

    def test_a_non_git_workspace_is_not_a_mismatch(self, workdir: Path) -> None:
        ws = workdir / "plain"
        ws.mkdir()
        config = config_for(workdir, FIELD_CONFIG.format(workspace=ws))
        assert workspace_origin_mismatches(config) == []  # type: ignore[arg-type]

    def test_ssh_and_dotgit_spellings_match(self, workdir: Path) -> None:
        ws = workdir / "repo"
        ws.mkdir()
        git_repo = Repo.init(ws)
        git_repo.create_remote("origin", "git@github.com:BrettBergin/lantern-backend.git")
        git_repo.close()
        config = config_for(
            workdir,
            f'[sandbox]\nworkspace = "{ws}"\n\n[[github.repos]]\n'
            'repo = "brettbergin/lantern-backend"\n'
            '[[github.repos]]\nrepo = "brettbergin/entrygraph"\n',
        )
        repos = {m.repo for m in workspace_origin_mismatches(config)}  # type: ignore[arg-type]
        assert repos == {"brettbergin/entrygraph"}


class TestDaemonStartRefusesOnOriginMismatch:
    def test_daemon_start_refuses_for_the_field_configuration(self, workdir: Path) -> None:
        ws = make_checkout(workdir / "repo", "brettbergin/lantern-backend")
        (workdir / "lantern.toml").write_text(FIELD_CONFIG.format(workspace=ws))
        result = runner.invoke(app, ["daemon", "--once"])
        assert result.exit_code == 2
        assert "daemon.workspace_origin_mismatch" in result.output
        assert "brettbergin/entrygraph" in result.output
        assert "brettbergin/lantern-backend" in result.output

    def test_daemon_start_passes_the_check_for_a_correct_single_repo(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = make_checkout(workdir / "repo", "brettbergin/lantern-backend")
        (workdir / "lantern.toml").write_text(
            f'[sandbox]\nworkspace = "{ws}"\n\n[[github.repos]]\n'
            'repo = "brettbergin/lantern-backend"\n'
        )
        from lantern.daemon.github import DaemonGithub
        from lantern.daemon.sources import GitHubIssueSource

        monkeypatch.setattr(DaemonGithub, "remove_stale", lambda self: None)
        monkeypatch.setattr(GitHubIssueSource, "poll", lambda self: [])
        result = runner.invoke(app, ["daemon", "--once"])
        assert "daemon.workspace_origin_mismatch" not in result.output

    def test_daemon_start_passes_the_check_for_per_repo_workspaces(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = make_checkout(workdir / "a", "brettbergin/lantern-backend")
        b = make_checkout(workdir / "b", "brettbergin/entrygraph")
        (workdir / "lantern.toml").write_text(
            f'[[github.repos]]\nrepo = "brettbergin/lantern-backend"\nworkspace = "{a}"\n'
            f'[[github.repos]]\nrepo = "brettbergin/entrygraph"\nworkspace = "{b}"\n'
        )
        from lantern.daemon.github import DaemonGithub
        from lantern.daemon.sources import GitHubIssueSource

        monkeypatch.setattr(DaemonGithub, "remove_stale", lambda self: None)
        monkeypatch.setattr(GitHubIssueSource, "poll", lambda self: [])
        result = runner.invoke(app, ["daemon", "--once"])
        assert "daemon.workspace_origin_mismatch" not in result.output
