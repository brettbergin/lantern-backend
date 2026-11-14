"""The generic deploy workflow, `contrib/workflows/deploy-daemon.yml.example`,
and the deploy guides (#534, #619, #639, #640, #642, #895).

The pipeline runs unattended on a root-equivalent host and cannot be
exercised here, so its load-bearing lines are pinned as text: no job on the
self-hosted runner may be reachable from a fork, its rollback must install
the same extras as its upgrade, it must read the daemon through
`ctl status --json` / `daemon notify` rather than prose, secrets and config
files, and nothing in it may name a host or a user.

This repository's own copy, `.github/workflows/deploy.yml`, is gone: no
Actions runner was left to run it. Lantern's own deploy is
`scripts/deploy_daemon.py` on another runner (docs/self-deploy.md), and
`tests/unit/test_deploy_daemon.py` runs that script against a synthetic host.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "contrib" / "workflows" / "deploy-daemon.yml.example"

_EXTRAS_RE = re.compile(r"lantern[-_]backend(?:-\S*?\.whl)?\[([\w,]+)\]")


@pytest.fixture(scope="module")
def example() -> str:
    return EXAMPLE.read_text()


def _step(text: str, name: str) -> str:
    """The text of one step, from its `- name:` to the next step's."""
    pattern = rf"      - name: {re.escape(name)}\n(.*?)(?=\n      - name: |\Z)"
    match = re.search(pattern, text, re.S)
    assert match, f"step {name!r} missing"
    return match.group(1)


def test_the_example_fetches_both_releases_before_the_hold(example: str) -> None:
    fetch = _step(example, "Fetch the release wheels")
    assert '-m lantern.releases download "${VERSION}"' in fetch
    assert '-m lantern.releases download "${PREV}"' in fetch
    assert example.index("name: Fetch the release wheels") < example.index(
        "name: Take the deploy hold"
    )
    assert "${DIST}/${PREV}/lantern_worker-${PREV}-py3-none-any.whl" in _step(example, "Roll back")


@pytest.mark.parametrize("fixture", ["example"])
def test_our_packages_are_never_installed_by_name(
    fixture: str, request: pytest.FixtureRequest
) -> None:
    """Only release wheel files: the names a rename moves to are not ours on
    any package index, so a by-name install could fetch someone else's code."""
    text = request.getfixturevalue(fixture)
    assert not re.search(r"""["']lantern(?:-worker)?(?:\[[\w,]*\])?==""", text)
    assert "pypi.org" not in text


@pytest.mark.parametrize("fixture", ["example"])
class TestAuthenticationOutage:
    def test_preflight_precedes_any_hold_or_upgrade(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        preflight = _step(text, "Check the host before upgrading")
        assert '"${LANTERN}" doctor' in preflight
        assert text.index("name: Check the host before upgrading") < text.index(
            "name: Take the deploy hold"
        )

    @pytest.mark.parametrize("failure", ["doctor", "status", "active", "none"])
    def test_rollback_only_reports_restored_after_health_checks(
        self,
        fixture: str,
        request: pytest.FixtureRequest,
        tmp_path: Path,
        failure: str,
    ) -> None:
        text = request.getfixturevalue(fixture)
        rollback = _step(text, "Roll back")
        script = textwrap.dedent(rollback.split("        run: |\n", 1)[1])
        for command in ("lantern", "uv", "systemctl", "sleep", "python"):
            path = tmp_path / command
            path.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  doctor) [ "$FAILURE" != doctor ] || exit 1;;\n'
                '  --version) echo "lantern $PREV";;\n'
                '  daemon) [ "$FAILURE" != status ] || exit 1;;\n'
                '  --user) if [ "$2" = is-active ] && [ "$FAILURE" = active ]; then exit 1; fi;;\n'
                "esac\n"
                "exit 0\n"
            )
            path.chmod(0o755)
        output = tmp_path / "output"
        env = {
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "LANTERN": str(tmp_path / "lantern"),
            "VENV_LANTERN": str(tmp_path / "lantern"),
            "VENV_PYTHON": str(tmp_path / "python"),
            "PIPELINE": "release_pipeline.py",
            "ACTIONS_TOKEN": "actions-token",
            "DIST": str(tmp_path / "dist"),
            "UV": str(tmp_path / "uv"),
            "PREV": "1.5.51",
            "UNIT": "test-daemon",
            "HOST": "test-host",
            "GITHUB_OUTPUT": str(output),
            "FAILURE": failure,
        }
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        if failure == "none":
            assert result.returncode == 0, result.stderr
            assert "restored=true" in output.read_text()
        else:
            assert result.returncode != 0, result.stdout
            assert "restored=true" not in output.read_text()
            assert "needs a human" in result.stdout

    def test_report_uses_verified_rollback_outcome(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        report = _step(text, "Report")
        assert "steps.rollback.outputs.restored" in report
        assert "rollback could not restore health" in report


class TestNeverRestartUnderARun:
    @pytest.mark.parametrize("fixture", ["example"])
    def test_the_backup_label_carries_no_dots(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """The snapshot is taken by the lantern already installed, whose
        label rules must hold on every release: dashes, never the dots of
        the version."""
        text = request.getfixturevalue(fixture)
        upgrade = _step(text, "Upgrade")
        assert 'backup --label "deploy-${VERSION//./-}"' in upgrade
        assert 'backup --label "deploy-${VERSION}"' not in upgrade


def _triggers(text: str) -> dict[str, dict]:
    """A workflow's `on:` mapping. YAML 1.1 reads a bare `on` key as true."""
    workflow = yaml.safe_load(text)
    return workflow.get("on", workflow.get(True))


class TestSecurityInvariant:
    def test_the_example_has_no_fork_triggerable_event(self, example: str) -> None:
        on = example[example.index("\non:\n") : example.index("\nconcurrency:")]
        assert "schedule:" in on and "workflow_dispatch:" in on
        for event in ("pull_request", "pull_request_target", "issue_comment", "push"):
            assert f"\n  {event}:" not in on
        assert "uses: actions/checkout" not in example


class TestExampleTriggers:
    """The reusable pattern checks for a release on a schedule, and a person
    dispatches a version, or none for the latest."""

    def test_the_example_keeps_its_schedule(self, example: str) -> None:
        assert set(_triggers(example)) == {"schedule", "workflow_dispatch"}
        assert set(_triggers(example)["workflow_dispatch"]["inputs"]) == {"version"}


class TestRollbackExtrasParity:
    """#619: a rollback that drops a chat extra rolls a host on that backend
    back to a build that cannot start."""

    @pytest.mark.parametrize("fixture", ["example"])
    def test_rollback_installs_the_same_extras_as_the_upgrade(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        upgrade = _EXTRAS_RE.findall(_step(text, "Upgrade"))
        rollback = _EXTRAS_RE.findall(_step(text, "Roll back"))
        assert upgrade and rollback
        assert set(upgrade[0].split(",")) == set(rollback[0].split(",")) == {"discord", "slack"}


@pytest.mark.parametrize("fixture", ["example"])
@pytest.mark.parametrize("step", ["Upgrade", "Roll back"])
def test_deploy_preserves_the_operator_installed_sbx(
    fixture: str, step: str, request: pytest.FixtureRequest
) -> None:
    """Plain init would replace a newer runtime with Lantern's pinned sbx,
    including during rollback, when the newer runtime may have migrated
    its state. Refreshing the service must leave that runtime untouched."""
    text = request.getfixturevalue(fixture)
    commands = re.findall(r'^\s*"\$\{LANTERN\}" init (.*)$', _step(text, step), re.M)
    assert commands, f"{step} does not refresh the installed service"
    assert all("--no-sbx" in command.split() for command in commands)


class TestStructuredControl:
    """#639: the job reads the daemon through `ctl status --json` and
    speaks through Lantern's notifier — never prose, the secrets file or
    the daemon's config."""

    @pytest.mark.parametrize("fixture", ["example"])
    def test_no_step_reads_secrets_or_config(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        for forbidden in (
            "secrets.env",
            "lantern.toml",
            "DISCORD_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "discord.com/api",
            "slack.com/api",
            "grep -i '^current:'",
            "grep -i '^holds:'",
            "^paused:",
        ):
            assert forbidden not in text, forbidden

    @pytest.mark.parametrize("fixture", ["example"])
    def test_status_is_read_as_json_and_notices_go_through_notify(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        for name in ("Take the deploy hold", "Wait for the daemon to go idle"):
            assert "daemon ctl status --json" in _step(text, name), name
        assert 'jq -r "${CURRENT_JQ}"' in _step(text, "Wait for the daemon to go idle")
        for name in ("Announce", "Report"):
            step = _step(text, name)
            assert 'daemon notify "${MSG}"' in step, name
            # A chat outage must not fail (or roll back) a deploy.
            assert "continue-on-error: true" in step, name

    def test_generic_deploy_notices_keep_using_the_control_channel(self, example: str) -> None:
        assert "DEPLOY_CHANNEL" not in example
        assert "daemon notify --channel" not in example


def _script(text: str, name: str) -> str:
    """One step's shell body, dedented and ready to run under bash."""
    return textwrap.dedent(_step(text, name).split("        run: |\n", 1)[1])


def _resolve(
    text: str,
    tmp_path: Path,
    *,
    home: str,
    override: str | None = None,
    variable: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the isolated `Resolve host paths` snippet with a given host
    environment. `override` is the runner process's own LANTERN_HOME (unset
    when None); `variable` is the repository variable the step reads."""
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": home,
        "GITHUB_ENV": str(github_env),
        "LANTERN_HOME_VAR": variable,
    }
    if override is not None:
        env["LANTERN_HOME"] = override
    return subprocess.run(
        ["bash", "-c", _script(text, "Resolve host paths")],
        env=env,
        capture_output=True,
        text=True,
    )


def _resolved(result: subprocess.CompletedProcess[str], github_env: Path) -> dict[str, str]:
    assert result.returncode == 0, result.stderr or result.stdout
    written: dict[str, str] = {}
    for line in github_env.read_text().splitlines():
        key, _, value = line.partition("=")
        written[key] = value
    return written


#: Every path the job addresses, and where it sits under the home.
_DERIVED = {
    "LANTERN": "bin/lantern",
    "VENV_LANTERN": "venv/bin/lantern",
    "VENV_PYTHON": "venv/bin/python",
    "UV": "bin/uv",
    "UV_CACHE_DIR": "cache/uv",
    "UV_PYTHON_INSTALL_DIR": "python",
}


@pytest.mark.parametrize("fixture", ["example"])
class TestHomeOverride:
    """#895: an operator may install under a custom `LANTERN_HOME`. The job
    resolves that root once and derives every path from it, so the version
    check, backup, install, health check and rollback all address the
    installation that is actually there."""

    def test_no_override_resolves_to_the_documented_default(
        self, fixture: str, request: pytest.FixtureRequest, tmp_path: Path
    ) -> None:
        home = tmp_path / "user"
        result = _resolve(request.getfixturevalue(fixture), tmp_path, home=str(home))
        written = _resolved(result, tmp_path / "github_env")
        assert written["LANTERN_HOME"] == f"{home}/.lantern"
        for key, tail in _DERIVED.items():
            assert written[key] == f"{home}/.lantern/{tail}", key

    def test_a_custom_absolute_root_survives_resolution(
        self, fixture: str, request: pytest.FixtureRequest, tmp_path: Path
    ) -> None:
        custom = tmp_path / "srv" / "lantern"
        result = _resolve(
            request.getfixturevalue(fixture),
            tmp_path,
            home=str(tmp_path / "user"),
            override=str(custom),
        )
        written = _resolved(result, tmp_path / "github_env")
        assert written["LANTERN_HOME"] == str(custom)
        for key, tail in _DERIVED.items():
            assert written[key] == f"{custom}/{tail}", key

    def test_a_root_containing_spaces_is_preserved(
        self, fixture: str, request: pytest.FixtureRequest, tmp_path: Path
    ) -> None:
        custom = tmp_path / "two words" / "lantern home"
        result = _resolve(
            request.getfixturevalue(fixture),
            tmp_path,
            home=str(tmp_path / "user"),
            override=str(custom),
        )
        written = _resolved(result, tmp_path / "github_env")
        assert written["LANTERN_HOME"] == str(custom)
        assert written["LANTERN"] == f"{custom}/bin/lantern"

    def test_the_repository_variable_carries_the_home_to_the_runner(
        self, fixture: str, request: pytest.FixtureRequest, tmp_path: Path
    ) -> None:
        """A runner whose own environment says nothing still deploys to the
        chosen home when the repository variable names it."""
        custom = tmp_path / "srv" / "lantern"
        result = _resolve(
            request.getfixturevalue(fixture),
            tmp_path,
            home=str(tmp_path / "user"),
            variable=str(custom),
        )
        written = _resolved(result, tmp_path / "github_env")
        assert written["LANTERN_HOME"] == str(custom)
        assert written["VENV_PYTHON"] == f"{custom}/venv/bin/python"

    def test_a_tilde_root_expands_against_the_service_user(
        self, fixture: str, request: pytest.FixtureRequest, tmp_path: Path
    ) -> None:
        """`LANTERN_HOME=~/elsewhere` is what the secrets example shows, and
        the loader expands it against HOME — so the job must too, rather
        than reject a literal `~` as relative."""
        home = tmp_path / "user"
        result = _resolve(
            request.getfixturevalue(fixture), tmp_path, home=str(home), override="~/elsewhere"
        )
        written = _resolved(result, tmp_path / "github_env")
        assert written["LANTERN_HOME"] == f"{home}/elsewhere"

    @pytest.mark.parametrize("bad", ["relative/home", "./home", "/srv/two\nlines"])
    def test_a_root_it_cannot_address_stops_before_anything_is_touched(
        self, fixture: str, request: pytest.FixtureRequest, tmp_path: Path, bad: str
    ) -> None:
        """Fail closed: a relative root is a different directory in every
        step, and a newline would silently corrupt GITHUB_ENV."""
        result = _resolve(
            request.getfixturevalue(fixture),
            tmp_path,
            home=str(tmp_path / "user"),
            override=bad,
        )
        assert result.returncode != 0, result.stdout
        assert "::error::" in result.stdout
        assert (tmp_path / "github_env").read_text() == ""


class TestHostAgnostic:
    """#640: the host is one repository variable; nothing names a machine,
    a user or a home directory."""

    @pytest.mark.parametrize("fixture", ["example"])
    def test_no_usernames_home_directories_or_hostnames(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        assert "/home/" not in text
        assert "bergs" not in text
        assert "ssh " not in text
        resolve = _step(text, "Resolve host paths")
        # The launcher is derived from the resolved home, and the only home
        # spelled out is the default the resolution falls back to.
        assert 'echo "LANTERN=${root}/bin/lantern"' in resolve
        assert 'root="${HOME}/.lantern"' in resolve
        assert ".lantern/bin" not in resolve
        assert "WORKDIR" not in text and 'cd "${' not in text  # the home is the home
        assert "needs a human" in _step(text, "Roll back")
        assert "${HOST} needs a human" in _step(text, "Roll back")

    def test_the_example_names_nothing_at_all(self, example: str) -> None:
        assert "brettbergin" not in example
        assert not re.search(r"\bdb\b", example)
        assert "|| 'db'" not in example
        assert 'runs-on: [self-hosted, "${{ vars.LANTERN_DEPLOY_HOST }}"]' in example
        assert "-m lantern.releases latest" in _step(example, "Resolve the target version")


class TestDocsSplit:
    """#642: the generic guide names no host, user or repository; the
    systemd README's upgrade section leads with the manual path."""

    @pytest.fixture
    def guide(self) -> str:
        return (ROOT / "docs" / "deploy.md").read_text()

    @pytest.fixture
    def systemd_readme(self) -> str:
        return (ROOT / "contrib" / "systemd" / "README.md").read_text()

    def test_generic_guide_names_nothing(self, guide: str) -> None:
        assert "brettbergin" not in guide
        assert not re.search(r"\bdb\b", guide)
        assert "ssh " not in guide and "/home/" not in guide
        assert "deploy.yml" not in guide  # that is the self-deploy reference's
        assert "<owner>/<repo>" in guide and "LANTERN_DEPLOY_HOST" in guide

    def test_generic_guide_points_at_the_self_deploy_reference(self, guide: str) -> None:
        assert "self-deploy.md" in guide
        reference = (ROOT / "docs" / "self-deploy.md").read_text()
        assert "brettbergin/lantern-backend" in reference and "LANTERN_DEPLOY_HOST" in reference
        assert "#639" in reference  # the structured-status cutover note

    def test_systemd_upgrade_section_leads_with_the_manual_path(self, systemd_readme: str) -> None:
        section = systemd_readme.split("## Upgrading", 1)[1].split("\n## ", 1)[0]
        first_fence = section.split("```bash", 1)[1].split("```", 1)[0]
        # The release's own wheels, checked — never a by-name index install.
        assert "lantern init --no-sbx --version X.Y.Z" in first_fence
        assert "pip install" not in first_fence and "==X.Y.Z" not in first_fence
        assert "lantern backup" in first_fence
        assert "lantern init --systemd --no-sbx" in first_fence
        assert "ctl status --json" in first_fence
        assert "reset-failed lantern-daemon" in first_fence
        # The workflow is the optional afterthought, below the commands.
        assert section.index("deploy-daemon.yml.example") > section.index("reset-failed")
        assert "Automated, via" not in section

    def test_runner_unit_is_marked_self_deploy_only(self) -> None:
        unit = (ROOT / "contrib" / "systemd" / "github-runner.service").read_text()
        assert unit.startswith("# Only needed for the automated upgrade workflow")

    def test_the_guide_renders_the_runner_unit_rather_than_copying_it(
        self, guide: str, systemd_readme: str
    ) -> None:
        """#896: the recipe copied the template — from a relative path that
        only exists in a source checkout, and without filling in @RUNNER@, so
        the unit could not start. init renders it for the real directory."""
        section = guide.split("### The runner", 1)[1].split("\n### ", 1)[0]
        assert "cp contrib/" not in guide and "~/.config/systemd/user/" not in section
        assert 'lantern init --systemd --no-sbx --runner "$HOME/actions-runner"' in section
        # init enables, never starts, so the start is its own command.
        assert "systemctl --user start github-runner" in section
        assert "--now" not in section
        # …and the systemd README documents the same flag.
        assert "lantern init --systemd --runner ~/actions-runner" in systemd_readme

    def test_the_documented_command_leaves_no_placeholder_in_the_unit(self, tmp_path: Path) -> None:
        """The other half of #896: what that command renders is startable."""
        from lantern.homeinit import render_unit
        from lantern.paths import LanternHome

        runner = tmp_path / "actions-runner"
        text = render_unit(
            "github-runner.service", LanternHome(tmp_path / "home"), runner_dir=runner
        )
        assert f"WorkingDirectory={runner}" in text
        assert f"ExecStart={runner}/run.sh" in text
        assert not re.search(r"@[A-Z]+@", text)

    def test_cutover_note_lives_in_the_changelog(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text()
        assert "### 1.0 cutover" in changelog
        readme = (ROOT / "README.md").read_text()
        assert "CHANGELOG.md#10-cutover" in readme
