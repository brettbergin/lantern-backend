"""Where sbxloop's own releases live, and how a host fetches one.

Releases are GitHub Releases only. Each carries both wheels, both sdists and
``release-manifest.json`` — the SHA-256 of every file and the commit they were
built from (``RELEASING.md``). sbxloop and sbxloop-worker are installed from
those wheel files, checked against that manifest, and never by name from a
package index: the names a later rename moves to are not ours on PyPI, so a
by-name install there could fetch somebody else's code. Third-party
dependencies still resolve from the index as usual.

Downloads go to ``github.com/<repo>/releases/download/v<X>/<file>`` directly
rather than through the release's asset list: that URL is the asset's own
``browser_download_url``, and GitHub's release lookups have been seen to report
no embedded assets for a release whose files are all there.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import sbxloop
from sbxloop.errors import SbxloopError

REPOSITORY = "brettbergin/sbxloop"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DOWNLOAD_URL = f"https://github.com/{REPOSITORY}/releases/download/v{{version}}/{{name}}"
MANIFEST = "release-manifest.json"
#: Wheels are a few MB; this bounds a pathological response, not a real one.
MAX_DOWNLOAD_BYTES = 100_000_000
DOWNLOAD_TIMEOUT_S = 120

Fetcher = Callable[[str, Path], None]


class ReleaseError(SbxloopError):
    """A release's files could not be fetched or did not match its manifest."""


def stable_version(value: str) -> str:
    """``value`` when it is a stable ``X.Y.Z`` release version; raises otherwise."""
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value or ""):
        raise ReleaseError(f"not a stable X.Y.Z release version: {value!r}")
    return value


def wheel_name(distribution: str, version: str) -> str:
    return f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"


def download_url(version: str, name: str) -> str:
    return DOWNLOAD_URL.format(version=stable_version(version), name=name)


@dataclass(frozen=True)
class ReleaseWheels:
    """The two wheel files of one release, ready for ``uv pip install``."""

    version: str
    host: Path
    worker: Path

    def requirements(self, extras: str) -> list[str]:
        """Both files as install arguments. Naming the local worker wheel
        satisfies the host wheel's exact ``sbxloop-worker==X`` pin, so the
        index is never consulted for either of our packages."""
        host = f"{self.host}[{extras}]" if extras else str(self.host)
        return [str(self.worker), host]


def wheel_paths(version: str, directory: Path) -> ReleaseWheels:
    """Where a release's two wheels sit in ``directory`` (not checked)."""
    return ReleaseWheels(
        version=version,
        host=directory / wheel_name("sbxloop", version),
        worker=directory / wheel_name("sbxloop-worker", version),
    )


def fetch_file(url: str, target: Path) -> None:
    """Stream ``url`` to ``target``, refusing anything over the size cap."""
    request = urllib.request.Request(url, headers={"User-Agent": f"sbxloop/{sbxloop.__version__}"})
    received = 0
    partial = target.with_name(f".{target.name}.partial")
    try:
        # nosec B310 - DOWNLOAD_URL is a constant https:// literal
        with (
            urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as source,  # nosec B310
            partial.open("wb") as out,
        ):
            for chunk in iter(lambda: source.read(1 << 20), b""):
                received += len(chunk)
                if received > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"response over {MAX_DOWNLOAD_BYTES} bytes")
                out.write(chunk)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_wheels(version: str, directory: Path) -> ReleaseWheels:
    """Both wheels in ``directory``, checked against its release manifest.

    Fails closed: a missing or malformed manifest, one for another version,
    or a wheel whose SHA-256 differs is an error, never a warning.
    """
    version = stable_version(version)
    wheels = wheel_paths(version, directory)
    try:
        manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"could not read {MANIFEST} for v{version}: {exc}") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(files, dict)
        or manifest.get("schema") != 1
        or manifest.get("version") != version
    ):
        raise ReleaseError(f"{MANIFEST} is not the manifest of v{version}")
    for path in (wheels.worker, wheels.host):
        expected = files.get(path.name)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ReleaseError(f"{MANIFEST} for v{version} has no SHA-256 for {path.name}")
        if not path.is_file():
            raise ReleaseError(f"missing {path.name} for v{version}")
        if _sha256(path) != expected:
            raise ReleaseError(
                f"{path.name} does not match the SHA-256 in the v{version} release manifest"
            )
    return wheels


def download_wheels(version: str, directory: Path, *, fetch: Fetcher = fetch_file) -> ReleaseWheels:
    """Download a release's manifest and both wheels into ``directory``
    (emptied first), then verify the wheels against the manifest."""
    version = stable_version(version)
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    wheels = wheel_paths(version, directory)
    for name in (MANIFEST, wheels.worker.name, wheels.host.name):
        url = download_url(version, name)
        try:
            fetch(url, directory / name)
        except (OSError, ValueError) as exc:
            raise ReleaseError(f"could not download {url}: {exc}") from exc
    return verify_wheels(version, directory)


def extra_install_hint(extra: str, fallback: str) -> str:
    """How to add an optional extra's third-party packages to this host.

    Names the packages themselves — read from the installed sbxloop's own
    metadata, so the constraints are the ones this release declares — never
    ``sbxloop[extra]``, which would send pip to an index for our own name.
    ``fallback`` stands in when the metadata cannot be read.
    """
    try:
        requires = metadata.requires("sbxloop") or []
    except metadata.PackageNotFoundError:
        requires = []
    wanted = re.compile(rf"""\bextra\s*==\s*["']{re.escape(extra)}["']""")
    found = [
        line.split(";", 1)[0].strip()
        for line in requires
        if ";" in line and wanted.search(line.split(";", 1)[1])
    ]
    packages = " ".join(f"'{item}'" for item in (found or [fallback]))
    return f"install {packages} into the venv sbxloop runs from"


USAGE = "usage: python -m sbxloop.releases latest | download X.Y.Z DIR"


def main(argv: list[str] | None = None) -> int:
    """A deploy pipeline's entry point, run with the installed sbxloop.

    ``latest`` prints the newest published release's version; ``download
    X.Y.Z DIR`` fetches that release's wheels into ``DIR``, checks them
    against its manifest, and prints the worker and host wheel paths.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args == ["latest"]:
        from sbxloop.daemon.versions import fetch_latest

        latest = fetch_latest("sbxloop")
        if latest is None:
            sys.stderr.write("error: could not read the latest release from GitHub\n")
            return 1
        sys.stdout.write(f"{latest}\n")
        return 0
    if len(args) == 3 and args[0] == "download":
        try:
            wheels = download_wheels(args[1], Path(args[2]))
        except ReleaseError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1
        sys.stdout.write(f"{wheels.worker}\n{wheels.host}\n")
        return 0
    sys.stderr.write(f"{USAGE}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
