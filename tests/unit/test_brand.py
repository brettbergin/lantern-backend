"""The product is Lantern: the old names appear only where they must.

sbxloop became Lantern and Angie became the Lantern web app with no
compatibility aliases. What may still say the old names: point-in-time
records (the changelog, spikes) and the one-time migration from an sbxloop
home, which has to name what it migrates from.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD = re.compile(rb"sbxloop|angie", re.IGNORECASE)
ALLOWED = {
    "CHANGELOG.md",
    ".github/workflows/cutover-from-sbxloop.yml",
    ".github/workflows/deploy.yml",
    "docs/self-deploy.md",
    "packages/lantern/src/lantern/cli/app.py",
    "packages/lantern/src/lantern/fromsbxloop.py",
    "tests/unit/test_brand.py",
    "tests/unit/test_from_sbxloop.py",
}
ALLOWED_PREFIXES = ("docs/spikes/",)


def test_old_names_appear_only_where_they_must() -> None:
    files = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.split(b"\0")
    offenders = []
    for raw in files:
        name = raw.decode("utf-8", "surrogateescape")
        if not name or name in ALLOWED or name.startswith(ALLOWED_PREFIXES):
            continue
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            continue
        if OLD.search(name.encode("utf-8", "surrogateescape")) or OLD.search(path.read_bytes()):
            offenders.append(name)
    assert offenders == []
