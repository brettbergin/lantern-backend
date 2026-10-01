"""``lantern init --from-sbxloop``: an sbxloop host's config becomes Lantern's."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lantern.cli.app import app
from lantern.fromsbxloop import FromSbxloopError, carry, default_source, rewrite
from lantern.paths import LanternHome

PEM = b"-----BEGIN RSA PRIVATE KEY-----\nsbxloopAAAA\n-----END RSA PRIVATE KEY-----\n"


def sbxloop_home(root: Path) -> Path:
    config = root / "config"
    config.mkdir(parents=True)
    (config / "sbxloop.toml").write_text(
        '[daemon]\ntrigger_label = "sbxloop"\n\n'
        '[[github.repos]]\nrepo = "octo/app"\n'
        f'workspace = "{root}/workspaces/octo/app"\n\n'
        "# BEGIN ANSIBLE MANAGED BLOCK sbxloop_oidc [api.oidc]\n"
        '[api.oidc]\nclient_id = "angie"\nclient_secret_env = "SBXLOOP_OIDC_CLIENT_SECRET"\n'
        "# END ANSIBLE MANAGED BLOCK sbxloop_oidc [api.oidc]\n"
    )
    secrets = config / "secrets.env"
    secrets.write_text(
        'SBXLOOP_OIDC_CLIENT_SECRET="s3cret"\n'
        f"GITHUB_APP_PRIVATE_KEY_PATH={root}/config/github-app.pem\n"
        "DISCORD_BOT_TOKEN=abc\n"
    )
    secrets.chmod(0o600)
    (config / "github-app.pem").write_bytes(PEM)
    checkout = root / "workspaces" / "octo" / "app"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (checkout / "README.md").write_text("hello\n")
    return root


def test_rewrite_renames_every_spelling() -> None:
    assert rewrite("SBXLOOP_HOME=~/.sbxloop # Sbxloop") == "LANTERN_HOME=~/.lantern # Lantern"


def test_a_pinned_sbx_app_name_keeps_its_value(tmp_path: Path) -> None:
    source = sbxloop_home(tmp_path / ".sbxloop")
    config = source / "config" / "sbxloop.toml"
    config.write_text(config.read_text() + '\n[sbx]\napp_name = "sbxloop"\n')
    secrets = source / "config" / "secrets.env"
    secrets.write_text(secrets.read_text() + "SBXLOOP_SBX__APP_NAME=sbxloop\n")
    home = LanternHome(tmp_path / ".lantern")

    carry(home, source)

    assert 'app_name = "sbxloop"' in (home.config / "lantern.toml").read_text()
    assert "LANTERN_SBX__APP_NAME=sbxloop\n" in (home.config / "secrets.env").read_text()


def test_carries_config_secrets_key_and_workspaces(tmp_path: Path) -> None:
    source = sbxloop_home(tmp_path / ".sbxloop")
    home = LanternHome(tmp_path / ".lantern")

    report = carry(home, source)

    config = home.config / "lantern.toml"
    text = config.read_text()
    assert 'trigger_label = "lantern"' in text
    assert f'workspace = "{tmp_path}/.lantern/workspaces/octo/app"' in text
    assert "BEGIN ANSIBLE MANAGED BLOCK lantern_oidc [api.oidc]" in text
    assert 'client_secret_env = "LANTERN_OIDC_CLIENT_SECRET"' in text
    # The identity provider's client is not ours to rename here.
    assert 'client_id = "angie"' in text
    secrets = home.config / "secrets.env"
    assert secrets.read_text() == (
        'LANTERN_OIDC_CLIENT_SECRET="s3cret"\n'
        f"GITHUB_APP_PRIVATE_KEY_PATH={tmp_path}/.lantern/config/github-app.pem\n"
        "DISCORD_BOT_TOKEN=abc\n"
    )
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    pem = home.config / "github-app.pem"
    assert pem.read_bytes() == PEM
    assert stat.S_IMODE(pem.stat().st_mode) == 0o600
    checkout = home.workspaces / "octo" / "app"
    assert (checkout / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert len(report.carried) == 4
    # The source is a rollback: nothing in it moved.
    assert (source / "config" / "sbxloop.toml").is_file()
    assert (source / "workspaces" / "octo" / "app" / "README.md").is_file()


def test_carrying_again_keeps_what_the_home_has(tmp_path: Path) -> None:
    source = sbxloop_home(tmp_path / ".sbxloop")
    home = LanternHome(tmp_path / ".lantern")
    carry(home, source)
    (home.config / "lantern.toml").write_text("# edited\n")

    report = carry(home, source)

    assert (home.config / "lantern.toml").read_text() == "# edited\n"
    assert report.carried == []
    assert len(report.kept) == 4


def test_refuses_a_directory_that_is_not_an_sbxloop_home(tmp_path: Path) -> None:
    with pytest.raises(FromSbxloopError, match="not an sbxloop home"):
        carry(LanternHome(tmp_path / ".lantern"), tmp_path / "elsewhere")


def test_refuses_to_carry_a_home_into_itself(tmp_path: Path) -> None:
    root = sbxloop_home(tmp_path / "shared")
    with pytest.raises(FromSbxloopError, match="same directory"):
        carry(LanternHome(root), root)


def test_default_source(tmp_path: Path) -> None:
    assert default_source({"HOME": str(tmp_path)}) == tmp_path / ".sbxloop"
    assert default_source({"HOME": "/x", "SBXLOOP_HOME": str(tmp_path / "s")}) == tmp_path / "s"


def test_init_dry_run_names_the_carry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = sbxloop_home(tmp_path / ".sbxloop")
    monkeypatch.setenv("LANTERN_HOME", str(tmp_path / ".lantern"))

    result = CliRunner().invoke(
        app, ["init", "--dry-run", "--no-sbx", "--from-sbxloop", str(source)]
    )

    assert f"would carry the sbxloop home {source}" in result.output
    assert not (tmp_path / ".lantern" / "config" / "lantern.toml").exists()
