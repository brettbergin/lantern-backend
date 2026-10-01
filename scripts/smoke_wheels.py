"""Install and exercise the exact release wheels outside the source checkout."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from importlib.metadata import requires, version
from pathlib import Path


def check_installed(dist: Path, expected: str) -> None:
    import lantern
    import lantern_worker
    from lantern.worker.wheel import resolve_worker_wheel

    if any(version(name) != expected for name in ("lantern-backend", "lantern-worker")):
        raise ValueError("installed distribution versions do not match the release")
    if lantern.__version__ != expected or lantern_worker.__version__ != expected:
        raise ValueError("imported package versions do not match the release")
    if f"lantern-worker=={expected}" not in (requires("lantern-backend") or []):
        raise ValueError("host wheel does not pin its matching worker")
    bundled = resolve_worker_wheel()
    worker = dist / f"lantern_worker-{expected}-py3-none-any.whl"
    if bundled is None or bundled.parent.name != "_vendor":
        raise ValueError("installed host did not resolve its vendored worker wheel")
    if (
        hashlib.sha256(bundled.read_bytes()).digest()
        != hashlib.sha256(worker.read_bytes()).digest()
    ):
        raise ValueError("vendored worker differs from the release worker wheel")


def smoke(dist: Path, expected: str) -> None:
    dist = dist.resolve()
    wheels = [
        dist / f"{name}-{expected}-py3-none-any.whl"
        for name in ("lantern_backend", "lantern_worker")
    ]
    if set(dist.glob("*.whl")) != set(wheels):
        raise ValueError("expected exactly the matching host and worker release wheels")
    with tempfile.TemporaryDirectory(prefix="lantern-wheel-smoke-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"}
        }

        def run(*args: str) -> str:
            return subprocess.run(
                args, cwd=root, env=env, check=True, text=True, capture_output=True, timeout=300
            ).stdout.strip()

        run("uv", "venv", "--python", "3.13", str(environment))
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = str(scripts / ("python.exe" if os.name == "nt" else "python"))
        run("uv", "pip", "install", "--python", python, *(str(path) for path in wheels))
        run(python, "-I", str(Path(__file__).resolve()), "--installed", str(dist), expected)
        result = run(str(scripts / ("lantern.exe" if os.name == "nt" else "lantern")), "--version")
        if result != f"lantern {expected}":
            raise ValueError(f"unexpected CLI version: {result}")
        print(f"Clean wheel smoke passed: {result}")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--installed":
            check_installed(Path(sys.argv[2]), sys.argv[3])
        else:
            smoke(Path(sys.argv[1]), sys.argv[2])
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"::error::wheel smoke: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
