"""``lantern init --from-sbxloop``: carry an sbxloop home into the Lantern home.

sbxloop became Lantern with no compatibility aliases: the package, CLI,
environment prefix, config file and home all changed name. A host that ran
sbxloop has everything under ``~/.sbxloop``; this module copies the parts a
new Lantern home needs and leaves the source untouched, so the old
installation stays a working rollback until it is removed by hand.

What is carried:

- ``config/``: every file, renamed (``sbxloop.toml`` -> ``lantern.toml``).
  Text files (TOML, env, shell) have ``sbxloop`` rewritten to ``lantern`` —
  paths under ``~/.sbxloop``, ``SBXLOOP_*`` variable names, Ansible block
  markers, labels — except a pinned sbx app name, whose logins live outside
  the home under that name; ``secrets.env`` keeps its private mode. Anything else
  (an App's private key) is copied byte for byte.
- ``workspaces/``: the repository checkouts the config points at, copied with
  their history, because the config's ``workspace`` paths now name the new home.

What is not: the state database, run directories, logs, caches, the venv and
the sandbox runtime. Lantern starts from fresh state; the installer builds the
rest.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lantern.errors import LanternError
from lantern.hostfiles import make_private
from lantern.paths import LanternHome
from lantern.releases import REPOSITORY

#: Files whose content names the old product and is rewritten.
TEXT_SUFFIXES = frozenset({".toml", ".env", ".sh", ".example"})
_PRIVATE_SUFFIXES = frozenset({".env", ".pem", ".key"})
#: Repositories that moved with the rename. A config naming one (and the
#: workspace checkout of it) follows it; everything else keeps its name.
REPOSITORIES = {f"{REPOSITORY.split('/')[0]}/sbxloop": REPOSITORY}
_REPOSITORY = re.compile(
    "|".join(rf"(?<![\w.-]){re.escape(old)}(?![\w-])" for old in REPOSITORIES), re.IGNORECASE
)
_REWRITES = (
    (re.compile(r"SBXLOOP"), "LANTERN"),
    (re.compile(r"Sbxloop"), "Lantern"),
    (re.compile(r"sbxloop"), "lantern"),
)


class FromSbxloopError(LanternError):
    pass


@dataclass
class CarryReport:
    carried: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


# sbx keeps its logins and sandboxes per ``--app-name`` outside the home, so a
# pinned app name keeps its value: renamed, it would name an app that has
# never logged in. Only the setting's own name changes.
_APP_NAME_TOML = re.compile(r"^\s*app_name\s*=")
_APP_NAME_ENV = re.compile(r"^(\s*(?:export\s+)?)(SBXLOOP_\w*APP_NAME)(\s*=.*)$")


def _rewrite_line(line: str) -> str:
    if _APP_NAME_TOML.match(line):
        return line
    body = line.rstrip("\r\n")
    env = _APP_NAME_ENV.match(body)
    if env:
        return env.group(1) + rewrite(env.group(2)) + env.group(3) + line[len(body) :]
    return rewrite(line)


def rename_repositories(text: str) -> str:
    """Every mention of a repository that moved, under its new name."""
    return _REPOSITORY.sub(lambda m: REPOSITORIES[m.group(0).lower()], text)


def rewrite(text: str) -> str:
    """The old product's names in a config or secrets file, renamed."""
    text = rename_repositories(text)
    for pattern, replacement in _REWRITES:
        text = pattern.sub(replacement, text)
    return text


def rewrite_file_text(text: str) -> str:
    """:func:`rewrite` line by line, keeping a pinned sbx app name's value."""
    return "".join(_rewrite_line(line) for line in text.splitlines(keepends=True))


def _renamed(rel: Path) -> Path:
    return Path(*(rewrite(part) for part in rel.parts))


def _is_text(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in ("secrets.env", ".env")


def _private(path: Path) -> bool:
    return path.suffix in _PRIVATE_SUFFIXES or path.name in ("secrets.env", ".env")


def source_config(source: Path) -> Path:
    """The sbxloop home's config file, or a named reason it is not one."""
    config = source / "config" / "sbxloop.toml"
    if not config.is_file():
        raise FromSbxloopError(f"{source} is not an sbxloop home: {config} is missing")
    return config


def carry(home: LanternHome, source: Path) -> CarryReport:
    """Copy ``source``'s config and workspaces into ``home``. Idempotent:
    a file or workspace the home already has is kept, never overwritten."""
    source = source.expanduser().resolve()
    if source == home.root.resolve():
        raise FromSbxloopError("the sbxloop home and the Lantern home are the same directory")
    source_config(source)
    report = CarryReport()
    home.ensure_tree()

    config_root = source / "config"
    for path in sorted(config_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        target = home.config / _renamed(path.relative_to(config_root))
        if target.exists():
            report.kept.append(str(target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if _is_text(path):
            target.write_text(rewrite_file_text(path.read_text(encoding="utf-8")), encoding="utf-8")
            shutil.copymode(path, target)
        else:
            shutil.copy2(path, target)
        if _private(path):
            make_private(target, os_name=home.os_name)
        report.carried.append(f"{path} -> {target}")

    workspaces = source / "workspaces"
    if workspaces.is_dir():
        for owner in sorted(p for p in workspaces.iterdir() if p.is_dir()):
            for repo in sorted(p for p in owner.iterdir() if p.is_dir()):
                target = home.workspaces / rename_repositories(f"{owner.name}/{repo.name}")
                if target.exists():
                    report.kept.append(str(target))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(repo, target, symlinks=True)
                _follow_origin(target)
                report.carried.append(f"{repo} -> {target}")
    return report


def _follow_origin(checkout: Path) -> None:
    """Point a carried checkout's origin at its repository's new name, so the
    daemon's workspace-origin check matches the renamed config."""
    if not (checkout / ".git").exists():
        return
    git = ["git", "-C", str(checkout)]
    try:
        url = subprocess.run(  # nosec B603 - fixed argv
            [*git, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    renamed = rename_repositories(url.removesuffix(".git")) + (
        ".git" if url.endswith(".git") else ""
    )
    if renamed != url:
        subprocess.run(  # nosec B603 - fixed argv
            [*git, "remote", "set-url", "origin", renamed], check=True, timeout=30
        )


_SBX_VERSION = re.compile(r"\d+\.\d+\.\d+")


_CLIENT_VERSION = re.compile(r"^Client Version:\s*v?(\d+\.\d+\.\d+)\b", re.MULTILINE)


def sbx_version_of(source: Path) -> str | None:
    """The sbx release the sbxloop home ran.

    Docker's sandbox daemon is shared by every home on the host and refuses a
    client older than itself, so the Lantern home installs the same sbx the
    sbxloop home had rather than Lantern's own pin. The home's own sbx binary
    is asked first (sbx can update itself past the stamp init wrote), then the
    ``sbx/VERSION`` stamp. ``None`` when neither answers.
    """
    root = source.expanduser() / "sbx"
    binary = root / "bin" / "sbx"
    if binary.is_file():
        try:
            out = subprocess.run(  # nosec B603 - fixed argv, the home's own binary
                [str(binary), "version"],
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        found = _CLIENT_VERSION.search(out)
        if found:
            return found.group(1)
    try:
        stamped = (root / "VERSION").read_text(encoding="utf-8").strip().removeprefix("v")
    except OSError:
        return None
    return stamped if _SBX_VERSION.fullmatch(stamped) else None


def default_source(env: dict[str, str] | os._Environ[str]) -> Path:
    """Where an sbxloop host kept its home unless told otherwise."""
    configured = env.get("SBXLOOP_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(env.get("HOME", "").strip() or str(Path.home())) / ".sbxloop"
