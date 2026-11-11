"""`scripts/deploy_daemon.py`: lantern's own deploy as three entry points.

The contract is `.github/workflows/deploy.yml` and `docs/self-deploy.md`:
for every step of that workflow the script does the same thing under the
same condition, in the same order, with the same failure semantics, split
over `prepare`, `deploy` and `finish` for a runner that has no step
conditions. The wanted differences are the cooldown wait, no release
receipt, the token rule, the hold prefix and where the job's settings come
from.

Every test runs the real script against a synthetic host: a lantern home
whose `lantern` (launcher and venv), `uv`, `sbx`, `systemctl` and `gh` are
fakes that report each invocation (argv, environment, stdin) to this
process and return what the test scripted. The home's `venv/bin/python`
runs the real `scripts/release_pipeline.py` with the release downloads and
the installed notifier answered from here, and GitHub is the repository's
own fake. Nothing sleeps for real: the script's clock and sleep are
injected, and the helper's clock follows them.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shlex
import signal
import socketserver
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lantern.daemon.holds import hold_name
from tests.fakes.fake_github import FakeGithub

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy_daemon.py"
HELPER = ROOT / "scripts" / "release_pipeline.py"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the deploy drives a POSIX host: systemd, sh launchers"
)

SPEC = importlib.util.spec_from_file_location("deploy_daemon", SCRIPT)
assert SPEC and SPEC.loader
deploy_daemon = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deploy_daemon
SPEC.loader.exec_module(deploy_daemon)

OLD, NEW, NEWER = "3.0.1", "3.0.2", "3.0.3"
RUN_ID = "4242"
HOLD = f"deploy-{RUN_ID}"
RUN_URL = "https://runner.example.test/runs/4242"
CHANNEL = "deploy-channel-id"
TOKEN = "job-token-for-github-only"
HUB_SECRET = "hub-secret"
#: Every name the job's GitHub token may travel under.
TOKEN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "DEPLOY_GITHUB_TOKEN")
#: The commands that act on the host with the host's own credentials.
HOST_COMMANDS = ("lantern", "venv-lantern", "uv", "sbx", "systemctl")
#: The helper subcommands the workflow hands its token to.
TOKEN_MODES = {"deploy-check", "download-wheels", "verify-download", "deploy-start"}

BUSY = {"current": {"item_id": "o/r#7", "run_id": "run-7", "title": "Fix the thing"}}
CLAIMING = {"current": None, "claiming": "o/r#12"}

#: The notices, as the workflow words them (see the cross-check below).
NOTICES = {
    "deploying": (
        "\U0001f680 deploying lantern `{FROM}` \u2192 `{VERSION}` on `{HOST}`"
        " \u2014 [run]({RUN_URL})"
    ),
    "staged": (
        "\U0001f680 lantern `{FROM}` \u2192 `{VERSION}` staged on `{HOST}` \u2014 restart deferred"
        " until the daemon is idle (now: {CURRENT}); hold `{HOLD}` \u2014 [run]({RUN_URL})"
    ),
    "refreshed": "Deploy batch refreshed after draining: installing {VERSION} on {HOST}.",
    "no_upgrade": (
        "Deploy batch on {HOST} finished without an upgrade ({DECISION});"
        " daemon remains on {FROM}. [run]({RUN_URL})"
    ),
    "restarted": "\u2705 `{HOST}` restarted on lantern `{VERSION}`{waited} \u2014 [run]({RUN_URL})",
    "before_install": (
        "\u274c deploy of lantern `{VERSION}` to `{HOST}` **{RESULT}** before anything was"
        " installed \u2014 still on `{FROM}`, daemon untouched. [run]({RUN_URL})"
    ),
    "rolled_back": (
        "\u274c deploy of lantern `{VERSION}` to `{HOST}` **{RESULT}** \u2014 rolled back to"
        " `{FROM}`. [run]({RUN_URL})"
    ),
    "needs_human": (
        "\u274c deploy of lantern `{VERSION}` to `{HOST}` **{RESULT}** \u2014 rollback could not"
        " restore health; {HOST} needs a human. [run]({RUN_URL})"
    ),
}


def notice(kind: str, **values: str) -> tuple[str, str]:
    """One expected notice: the deploy channel and the workflow's wording."""
    defaults = {
        "FROM": OLD,
        "VERSION": NEW,
        "HOST": "test-host",
        "RUN_URL": RUN_URL,
        "HOLD": HOLD,
        "RESULT": "failure",
        "waited": "",
    }
    return CHANNEL, NOTICES[kind].format(**{**defaults, **values})


def files(version: str) -> list[str]:
    """What a release carries, as Release publishes it."""
    return [
        f"lantern_backend-{version}-py3-none-any.whl",
        f"lantern_backend-{version}.tar.gz",
        f"lantern_worker-{version}-py3-none-any.whl",
        f"lantern_worker-{version}.tar.gz",
    ]


# -- the synthetic host ------------------------------------------------------

#: What every fake executable runs: report the invocation, print the answer.
CLIENT = r"""
import json
import os
import socket
import sys


def ask(request):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "port")) as stream:
        port = int(stream.read())
    with socket.create_connection(("127.0.0.1", port), timeout=120) as connection:
        stream = connection.makefile("rwb")
        stream.write(json.dumps(request).encode() + b"\n")
        stream.flush()
        return json.loads(stream.readline() or b"{}")


if __name__ == "__main__":
    name, exe, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
    try:
        devnull = os.path.samestat(os.fstat(0), os.stat(os.devnull))
    except OSError:
        devnull = False
    reply = ask(
        {
            "cmd": name,
            "exe": exe,
            "argv": argv,
            "env": dict(os.environ),
            "stdin": sys.stdin.read() if "--password-stdin" in argv else None,
            "stdin_devnull": devnull,
        }
    )
    sys.stdout.write(reply.get("stdout", ""))
    sys.stderr.write(reply.get("stderr", ""))
    sys.exit(reply.get("rc", 1))
"""

#: The home's `venv/bin/python`: the real helper, on an interpreter whose
#: downloads, clock and installed notifier answer from the test.
HARNESS = r"""
import base64
import io
import os
import runpy
import sys
import time
import types
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import ask

exe, script, mode = sys.argv[1], sys.argv[2], sys.argv[3]
reply = ask({"cmd": "python", "exe": exe, "argv": [script, mode], "env": dict(os.environ)})
if reply.get("rc") is not None:
    sys.stderr.write(reply.get("stderr", ""))
    sys.exit(reply["rc"])
# Whatever the helper itself spawns says so, apart from what the script runs.
os.environ["FAKE_HELPER"] = mode
real_time = time.time
offset = reply.get("offset", 0)
time.time = lambda: real_time() + offset


def urlopen(url, timeout=None):
    answer = ask({"cmd": "urlopen", "exe": "", "argv": [url], "env": dict(os.environ)})
    if answer.get("rc"):
        raise OSError(answer.get("stderr") or "download failed")
    return io.BytesIO(base64.b64decode(answer["body"]))


urllib.request.urlopen = urlopen


class LanternError(Exception):
    pass


class Section:
    def __init__(self, channel_id):
        self.channel_id = channel_id

    def model_copy(self, update):
        return Section(update.get("channel_id", self.channel_id))


class Config:
    chat_backend = "mattermost"

    def __init__(self):
        self.mattermost = Section("the-control-channel")

    def chat_section(self, backend):
        return getattr(self, backend)

    def model_copy(self, update):
        copy = Config()
        for key, value in update.items():
            setattr(copy, key, value)
        return copy


class Posted:
    def __init__(self, backend, channel_id):
        self.backend = backend
        self.channel_id = channel_id


def post_notice(config, text):
    channel = config.mattermost.channel_id
    answer = ask({"cmd": "notify", "exe": "", "argv": [channel, text], "env": dict(os.environ)})
    if answer.get("rc"):
        raise LanternError(answer.get("stderr") or "chat is down")
    return Posted("mattermost", channel)


def module(name, **members):
    made = types.ModuleType(name)
    made.__dict__.update(members)
    sys.modules[name] = made


module("lantern")
module("lantern.daemon")
module("lantern.config", load_config=Config, load_secrets_env=lambda: None)
module("lantern.daemon.notify", post_notice=post_notice)
module("lantern.errors", LanternError=LanternError)

sys.argv = [script, mode]
runpy.run_path(script, run_name="__main__")
"""


@dataclass
class Call:
    """One invocation of a fake, as the fake saw it."""

    cmd: str
    exe: str
    argv: list[str]
    env: dict[str, str]
    stdin: str | None = None
    stdin_devnull: bool = False

    @property
    def line(self) -> str:
        if self.cmd == "python":
            return f"python {self.argv[1]}"
        line = " ".join([self.cmd, *self.argv])
        # The wheels sit in a directory of the script's choosing.
        return re.sub(r"\S*/(lantern_(?:worker|backend)-)", r"<dist>/\1", line)

    @property
    def via_helper(self) -> bool:
        """Spawned by release_pipeline.py itself, not by the script."""
        return "FAKE_HELPER" in self.env


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    host: Host


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        host = self.server.host  # type: ignore[attr-defined]
        reply = host.handle(json.loads(line))
        if reply.pop("block", False):
            host.blocked.set()
            host.unblock.wait(60)
        with contextlib.suppress(OSError):  # the caller was killed while it waited
            self.wfile.write(json.dumps(reply).encode() + b"\n")


@dataclass
class Host:
    """A daemon host: its home, its fakes, its GitHub, and what happened."""

    root: Path
    home: Path
    installed: str = OLD
    calls: list[Call] = field(default_factory=list)
    notices: list[tuple[str, str]] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)
    holds: set[str] = field(default_factory=set)
    released: list[str] = field(default_factory=list)
    assets: dict[str, bytes] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)
    download_failures: int = 0
    daemon_up: bool = True
    chat_down: bool = False
    restarts: int = 0
    #: Which `uv pip install` calls (1-based) exit 0 without installing.
    pip_noops: set[int] = field(default_factory=set)
    pip_calls: int = 0
    offset: float = 0.0
    on_sleep: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.github = FakeGithub()
        self.user = self.root / "user"
        self.user.mkdir()
        self.runner_temp = self.root / "runner-temp"
        self.runner_temp.mkdir()
        self.path = self.root / "path"
        self.fake = self.root / "fake"
        self.fake.mkdir()
        self._lock = threading.Lock()
        self._scripts: list[tuple[str, deque[Any]]] = []
        self._statuses: deque[Any] = deque()
        self._blocks: list[list[Any]] = []
        self.blocked = threading.Event()
        self.unblock = threading.Event()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.host = self
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        (self.fake / "port").write_text(str(self._server.server_address[1]))
        (self.fake / "client.py").write_text(CLIENT)
        (self.fake / "harness.py").write_text(HARNESS)
        for name, target in {
            "lantern": self.home / "bin" / "lantern",
            "venv-lantern": self.home / "venv" / "bin" / "lantern",
            "uv": self.home / "bin" / "uv",
            "sbx": self.home / "bin" / "sbx",
            "systemctl": self.path / "systemctl",
            "gh": self.path / "gh",
        }.items():
            self._executable(target, f"{shlex.quote(str(self.fake / 'client.py'))} {name}")
        self._executable(
            self.home / "venv" / "bin" / "python", shlex.quote(str(self.fake / "harness.py"))
        )
        (self.home / "state").mkdir()
        self.publish(OLD)
        self.publish(NEW)
        self.env: dict[str, str] = {
            "PATH": f"{self.path}:/usr/bin:/bin",
            "HOME": str(self.user),
            "LANTERN_HOME": str(self.home),
            "RUNNER_TEMP": str(self.runner_temp),
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": RUN_ID,
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_ENV": str(self.root / "github-env"),
            "GITHUB_OUTPUT": str(self.root / "github-output"),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
            "INPUT_VERSION": "",
            "HOST": "test-host",
            "UNIT": "test-daemon",
            "DEPLOY_CHANNEL": CHANNEL,
            "RUN_URL": RUN_URL,
            "DOCKERHUB_USERNAME": "hubuser",
            "DOCKERHUB_TOKEN": HUB_SECRET,
            "DEPLOY_GITHUB_TOKEN": TOKEN,
        }

    def _executable(self, target: Path, runs: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -S -E {runs} "$0" "$@"\n')
        target.chmod(0o755)

    def close(self) -> None:
        self.unblock.set()
        self._server.shutdown()
        self._server.server_close()

    # -- GitHub ---------------------------------------------------------------

    def publish(self, version: str) -> None:
        """A complete published release on main, manifest included."""
        sha = hashlib.sha1(f"commit {version}".encode(), usedforsecurity=False).hexdigest()
        contents = {name: f"{name} as published".encode() for name in files(version)}
        manifest = {
            "schema": 1,
            "version": version,
            "sha": sha,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
        }
        contents["release-manifest.json"] = json.dumps(manifest).encode()
        self.github.release_payloads.append(
            {
                "id": len(self.github.release_payloads) + 1,
                "tag_name": f"v{version}",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-09-12T00:00:00Z",
                "assets": [self._asset(version, name, data) for name, data in contents.items()],
            }
        )
        self.github.release_tags.append({"name": f"v{version}", "commit": {"sha": sha}})
        self.github.release_heads = [sha]

    def _asset(self, version: str, name: str, data: bytes) -> dict[str, Any]:
        url = f"https://github.com/o/r/releases/download/v{version}/{name}"
        self.assets[url] = data
        return {
            "name": name,
            "state": "uploaded",
            "size": len(data),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "browser_download_url": url,
        }

    def break_manifest(self, version: str) -> None:
        """The release's manifest no longer describes its wheels."""
        item = next(r for r in self.github.release_payloads if r["tag_name"] == f"v{version}")
        url = f"https://github.com/o/r/releases/download/v{version}/release-manifest.json"
        manifest = json.loads(self.assets[url])
        manifest["files"] = dict.fromkeys(manifest["files"], "0" * 64)
        item["assets"] = [a for a in item["assets"] if a["name"] != "release-manifest.json"]
        item["assets"].append(
            self._asset(version, "release-manifest.json", json.dumps(manifest).encode())
        )

    # -- scripting --------------------------------------------------------------

    def script(self, prefix: str, results: list[Any]) -> None:
        """Answer the next calls starting with *prefix* from *results*, in
        order: an exit code, `(exit code, stdout)`, or None for the usual
        behaviour. Calls after the list ends behave as usual."""
        self._scripts.append((prefix, deque(results)))

    def statuses(self, *answers: Any) -> None:
        """What the next `ctl status --json` calls say: a status object, or
        an exit code. After these the daemon is idle."""
        self._statuses.extend(answers)

    def block(self, prefix: str, *, skip: int = 0) -> None:
        """A call starting with *prefix* never returns: the first one, or
        the one after *skip* others."""
        self._blocks.append([prefix, skip])

    def during_first_sleep(self, action: Any) -> None:
        """Something that happens on the host while the script first waits."""
        pending = [action]
        self.on_sleep.append(lambda seconds: pending.pop()() if pending else None)

    def seed_deploy_state(self, **state: Any) -> None:
        full = {"finished_at": 0, "blocked_through": "", "in_progress": False, **state}
        self.deploy_json.write_text(json.dumps(full) + "\n")

    # -- the fakes ----------------------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            call = Call(
                request["cmd"],
                request.get("exe", ""),
                request["argv"],
                request.get("env", {}),
                request.get("stdin"),
                request.get("stdin_devnull", False),
            )
            self.calls.append(call)
            reply = self._scripted(call)
            if reply is None:
                reply = getattr(self, "_" + call.cmd.replace("-", "_"))(call)
            for entry in self._blocks:
                if call.line.startswith(entry[0]):
                    if entry[1] > 0:
                        entry[1] -= 1
                    else:
                        reply["block"] = True
            return reply

    def _scripted(self, call: Call) -> dict[str, Any] | None:
        for prefix, results in self._scripts:
            if (call.line == prefix or call.line.startswith(prefix + " ")) and results:
                result = results.popleft()
                if result is None:
                    return None
                if isinstance(result, tuple):
                    return {"rc": result[0], "stdout": result[1]}
                return {"rc": result}
        return None

    def _lantern(self, call: Call) -> dict[str, Any]:
        argv = call.argv
        if argv[:2] != ["daemon", "ctl"]:
            return {"rc": 0}
        if not self.daemon_up:
            return {"rc": 2, "stderr": "no daemon answered\n"}
        if argv[2] == "pause":
            self.holds.add(argv[argv.index("--hold") + 1])
        elif argv[2] == "resume":
            name = argv[argv.index("--hold") + 1]
            self.holds.discard(name)
            self.released.append(name)
        elif argv[2] == "status" and "--json" in argv:
            answer = self._statuses.popleft() if self._statuses else {}
            if isinstance(answer, int):
                return {"rc": answer, "stdout": "the daemon is busy\n" if answer == 1 else ""}
            if isinstance(answer, str):
                return {"rc": 0, "stdout": answer}
            status = {
                "current": None,
                "claiming": None,
                "holds": sorted(self.holds),
                "paused": bool(self.holds),
                "queued": 0,
                **answer,
            }
            return {"rc": 0, "stdout": json.dumps(status) + "\n"}
        elif argv[2] == "status":
            return {"rc": 0, "stdout": f"holds: {', '.join(sorted(self.holds)) or 'none'}\n"}
        return {"rc": 0}

    def _venv_lantern(self, call: Call) -> dict[str, Any]:
        return {"rc": 0, "stdout": f"lantern {self.installed}\n"}

    def _uv(self, call: Call) -> dict[str, Any]:
        self.pip_calls += 1
        wheels = [arg.split(".whl")[0] + ".whl" for arg in call.argv if ".whl" in arg]
        missing = [wheel for wheel in wheels if not Path(wheel).is_file()]
        if missing or len(wheels) != 2:
            return {"rc": 1, "stderr": f"no such wheel: {missing}\n"}
        if self.pip_calls not in self.pip_noops:
            found = re.search(r"lantern_backend-(\d+\.\d+\.\d+)-", " ".join(wheels))
            assert found, wheels
            self.installed = found[1]
        return {"rc": 0}

    def _systemctl(self, call: Call) -> dict[str, Any]:
        if "show" in call.argv:
            return {"rc": 0, "stdout": "0\n"}
        if "restart" in call.argv:
            self.restarts += 1
        return {"rc": 0}

    def _sbx(self, call: Call) -> dict[str, Any]:
        return {"rc": 0}

    def _gh(self, call: Call) -> dict[str, Any]:
        if call.argv[:1] != ["api"] or len(call.argv) != 2:
            return {"rc": 1, "stderr": f"fake gh: not modelled: {call.argv}\n"}
        try:
            payload = self.github.release_request(call.argv[1])
        except Exception as error:
            return {"rc": 1, "stderr": f"{error}\n"}
        return {"rc": 0, "stdout": json.dumps(payload)}

    def _python(self, call: Call) -> dict[str, Any]:
        return {"rc": None, "offset": self.offset}

    def _urlopen(self, call: Call) -> dict[str, Any]:
        url = call.argv[0]
        if self.download_failures > 0:
            self.download_failures -= 1
            return {"rc": 1, "stderr": "connection reset"}
        if url not in self.assets or any(f"/v{version}/" in url for version in self.unavailable):
            return {"rc": 1, "stderr": "404"}
        return {"rc": 0, "body": base64.b64encode(self.assets[url]).decode()}

    def _notify(self, call: Call) -> dict[str, Any]:
        if self.chat_down:
            return {"rc": 1, "stderr": "chat is down"}
        self.notices.append((call.argv[0], call.argv[1]))
        return {"rc": 0}

    # -- running the script -----------------------------------------------------

    def environment(self, changes: dict[str, str | None]) -> dict[str, str]:
        env = dict(self.env)
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.offset += seconds
        for hook in self.on_sleep:
            hook(seconds)

    def clock(self) -> float:
        return time.time() + self.offset

    def run(self, *argv: str, **env: str | None) -> int:
        """One entry point, in this process, on the injected environment."""
        return deploy_daemon.main(
            list(argv), env=self.environment(env), sleep=self.sleep, clock=self.clock
        )

    def spawn(self, *argv: str, **env: str | None) -> subprocess.Popen[str]:
        """One entry point as the runner would start it: its own process."""
        return subprocess.Popen(
            [sys.executable, str(SCRIPT), *argv],
            env=self.environment(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    def kill_when_blocked(self, process: subprocess.Popen[str]) -> None:
        """SIGKILL the entry point and everything it started, mid-call."""
        assert self.blocked.wait(60), "the script never reached the blocking call"
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(30)
        self.unblock.set()
        self._blocks.clear()

    def prepared(self, *argv: str, **env: str | None) -> None:
        """`prepare` has run and found a deploy to do; start counting here."""
        assert self.run("prepare", *argv, **env) == 0
        self.forget()

    def forget(self) -> None:
        self.calls.clear()
        self.slept.clear()
        self.notices.clear()

    # -- what happened ------------------------------------------------------------

    def lines(self) -> list[str]:
        """The host commands the script ran, in order."""
        return [c.line for c in self.calls if c.cmd in HOST_COMMANDS and not c.via_helper]

    def modes(self) -> list[str]:
        """The helper subcommands the script ran, in order."""
        return [c.argv[1] for c in self.calls if c.cmd == "python"]

    def helper(self, mode: str, index: int = 0) -> Call:
        return [c for c in self.calls if c.cmd == "python" and c.argv[1] == mode][index]

    @property
    def deploy_json(self) -> Path:
        return self.home / "state" / "deploy.json"

    def deploy_state(self) -> dict[str, Any] | None:
        return json.loads(self.deploy_json.read_text()) if self.deploy_json.exists() else None

    def history(self) -> list[str]:
        log = self.home / "logs" / "deploy" / "history.log"
        return log.read_text().splitlines() if log.exists() else []

    def leftovers(self) -> list[str]:
        """What the job left in the runner's temporary directory."""
        return sorted(p.name for p in self.runner_temp.iterdir())


@pytest.fixture
def make_host(tmp_path: Path):
    made: list[Host] = []

    def make(home: Path) -> Host:
        made.append(Host(tmp_path, home))
        return made[-1]

    yield make
    for each in made:
        each.close()


@pytest.fixture
def host(make_host: Any, tmp_path: Path) -> Host:
    return make_host(tmp_path / "srv" / "lantern")


def prepare_lines(hold: str = HOLD) -> list[str]:
    return [
        "lantern doctor",
        "sbx --app-name lantern-analysis login --username hubuser --password-stdin",
        "sbx --app-name lantern-analysis policy init deny-all",
        "sbx --app-name lantern-analysis policy deny network **",
        "sbx --app-name lantern-analysis version",
        f"lantern daemon ctl pause --hold {hold} --timeout 30",
        "lantern daemon ctl status --json --timeout 30",
        "lantern daemon ctl status --json --timeout 30",
    ]


def install_line(host: Host, version: str) -> str:
    return (
        f"uv pip install --python {host.home}/venv/bin/python --upgrade"
        f" <dist>/lantern_worker-{version}-py3-none-any.whl"
        f" <dist>/lantern_backend-{version}-py3-none-any.whl[discord,slack]"
    )


RESTART = ["systemctl --user reset-failed test-daemon", "systemctl --user restart test-daemon"]
CHECKS = [
    "systemctl --user is-active --quiet test-daemon",
    "venv-lantern --version",
    "lantern doctor",
    "lantern daemon ctl status --json --timeout 300",
]


def forward_lines(host: Host, version: str = NEW, previous: str = OLD) -> list[str]:
    """Upgrade, restart and health check, as the workflow orders them."""
    label = version.replace(".", "-")
    return [
        f"lantern backup --label deploy-{label} --reason deploy {previous} -> {version}",
        install_line(host, version),
        "venv-lantern --version",
        "lantern init --systemd --no-sbx",
        *RESTART,
        *CHECKS,
        "systemctl --user show test-daemon -p NRestarts --value",
        "systemctl --user is-active --quiet test-daemon",
        "systemctl --user show test-daemon -p NRestarts --value",
    ]


def rollback_lines(host: Host, previous: str = OLD) -> list[str]:
    return [install_line(host, previous), "lantern init --systemd --no-sbx", *RESTART, *CHECKS]


def release_lines(hold: str = HOLD) -> list[str]:
    return [
        f"lantern daemon ctl resume --hold {hold} --timeout 60",
        "lantern daemon ctl status --timeout 60",
    ]


FETCH = ["deploy-check", "download-wheels", "verify-download", "deploy-start"]


# -- the decision ------------------------------------------------------------


def _current(host: Host) -> None:
    host.installed = NEW


def _older(host: Host) -> None:
    host.installed = NEWER


def _blocked(host: Host) -> None:
    host.seed_deploy_state(blocked_through=NEW)


def _cooldown(host: Host) -> None:
    host.seed_deploy_state(finished_at=int(time.time()) - 60)


class TestNothingToDeploy:
    """Cooldown, current, older and blocked releases exit before doctor or a
    pause hold; the later entry points then have nothing to do."""

    @pytest.mark.parametrize("arrange", [_current, _older, _blocked, _cooldown])
    def test_a_decision_not_to_deploy_changes_nothing(self, host: Host, arrange: Any) -> None:
        arrange(host)
        before = host.deploy_state()

        assert host.run("prepare") == 0
        # The decision is the helper's, asked with the job token and no receipt.
        assert host.modes() == ["deploy-check"]
        assert host.helper("deploy-check").argv[0] == str(HELPER)
        assert host.helper("deploy-check").env["MANUAL"] == "false"
        assert host.run("deploy") == 0
        assert host.run("finish", "--status", "success") == 0

        assert host.modes() == ["deploy-check"]
        assert host.lines() == []
        assert host.holds == set() and host.released == []
        assert host.notices == []
        assert host.deploy_state() == before
        assert host.history() == []
        assert host.leftovers() == []
        reason = arrange.__name__.strip("_")
        assert f"Deployment: {reason};" in Path(host.env["GITHUB_STEP_SUMMARY"]).read_text()

    def test_an_interrupted_deployment_stops_an_automatic_run(self, host: Host) -> None:
        host.seed_deploy_state(in_progress=True, blocked_through=NEW)
        before = host.deploy_state()

        assert host.run("prepare") != 0
        assert host.run("finish", "--status", "failure") == 0

        assert host.modes() == ["deploy-check"]
        assert host.lines() == [] and host.notices == [] and host.released == []
        assert host.deploy_state() == before
        assert host.leftovers() == []

    def test_a_github_outage_stops_before_the_host_is_touched(self, host: Host) -> None:
        host.github.release_api_error = OSError("unavailable")

        assert host.run("prepare") != 0

        assert host.lines() == [] and host.holds == set()

    def test_deploy_refuses_to_run_when_prepare_did_not(self, host: Host) -> None:
        assert host.run("deploy") != 0
        assert host.calls == []


# -- the home ----------------------------------------------------------------

#: Every path the job addresses, and where it sits under the home.
DERIVED = {
    "LANTERN": "bin/lantern",
    "VENV_LANTERN": "venv/bin/lantern",
    "VENV_PYTHON": "venv/bin/python",
    "UV": "bin/uv",
    "UV_CACHE_DIR": "cache/uv",
    "UV_PYTHON_INSTALL_DIR": "python",
}


def _addressed(host: Host, home: Path) -> None:
    """Every command ran from *home* and was told that home."""
    commands = [c for c in host.calls if c.cmd in ("lantern", "sbx", "python")]
    assert {c.cmd for c in commands} == {"lantern", "sbx", "python"}
    for call in commands:
        assert call.exe.startswith(f"{home}/"), call.line
        assert call.env["LANTERN_HOME"] == str(home), call.line
        for key, tail in DERIVED.items():
            assert call.env[key] == f"{home}/{tail}", (call.line, key)


class TestHome:
    """The home is resolved once, as the workflow's first step resolves it,
    and every path derives from it."""

    def test_no_override_resolves_to_the_default_home(self, make_host: Any, tmp_path: Path) -> None:
        host = make_host(tmp_path / "user" / ".lantern")
        assert host.run("prepare", LANTERN_HOME=None) == 0
        _addressed(host, tmp_path / "user" / ".lantern")

    def test_the_runner_s_own_home_is_used(self, host: Host) -> None:
        assert host.run("prepare") == 0
        _addressed(host, host.home)

    def test_the_variable_wins_over_the_runner_s_home(self, host: Host, tmp_path: Path) -> None:
        elsewhere = str(tmp_path / "not" / "this")
        assert host.run("prepare", LANTERN_HOME=elsewhere, LANTERN_HOME_VAR=str(host.home)) == 0
        _addressed(host, host.home)

    def test_a_tilde_root_expands_against_the_service_user(
        self, make_host: Any, tmp_path: Path
    ) -> None:
        host = make_host(tmp_path / "user" / "elsewhere")
        assert host.run("prepare", LANTERN_HOME="~/elsewhere") == 0
        _addressed(host, tmp_path / "user" / "elsewhere")

    def test_a_home_containing_spaces_deploys_as_itself(
        self, make_host: Any, tmp_path: Path
    ) -> None:
        host = make_host(tmp_path / "two words" / "lantern home")
        assert host.run("prepare") == 0
        assert host.run("deploy") == 0
        assert host.run("finish", "--status", "success") == 0
        _addressed(host, host.home)
        assert host.installed == NEW
        (install,) = [c for c in host.calls if c.cmd == "uv"]
        assert install.argv[:5] == [
            "pip",
            "install",
            "--python",
            f"{host.home}/venv/bin/python",
            "--upgrade",
        ]
        assert host.history() and host.notices[-1:] == [notice("restarted")]

    @pytest.mark.parametrize("bad", ["relative/home", "./home", "/srv/two\nlines"])
    def test_a_root_it_cannot_address_stops_before_anything_is_touched(
        self, host: Host, capfd: pytest.CaptureFixture[str], bad: str
    ) -> None:
        assert host.run("prepare", LANTERN_HOME=bad) != 0
        assert "::error::" in capfd.readouterr().out
        assert host.calls == []

    def test_a_home_with_nothing_installed_stops_before_anything_is_touched(
        self, host: Host, capfd: pytest.CaptureFixture[str]
    ) -> None:
        for installed in ("bin/lantern", "venv/bin/lantern", "venv/bin/python"):
            (host.home / installed).unlink()

        assert host.run("prepare") != 0

        assert "::error::" in capfd.readouterr().out
        assert host.calls == [] and host.leftovers() != []
        assert host.run("finish", "--status", "failure") == 0
        assert host.calls == [] and host.leftovers() == []


# -- prepare -----------------------------------------------------------------


class TestPrepare:
    def test_it_checks_the_host_takes_the_hold_announces_and_waits(self, host: Host) -> None:
        assert host.run("prepare") == 0

        assert host.modes() == ["deploy-check", "deploy-notify"]
        assert host.lines() == prepare_lines()
        assert host.holds == {HOLD}
        assert host.notices == [notice("deploying")]
        assert host.slept == []
        # Nothing of the upgrade yet: that is `deploy`'s.
        assert host.installed == OLD and host.restarts == 0 and host.deploy_state() is None

    def test_the_analyzer_signs_in_with_the_secret_on_stdin_only(self, host: Host) -> None:
        assert host.run("prepare") == 0

        logins = [c for c in host.calls if c.cmd == "sbx" and "login" in c.argv]
        assert len(logins) == 1
        (login,) = logins
        assert login.stdin == HUB_SECRET
        assert login.exe == f"{host.home}/bin/sbx"
        assert all(HUB_SECRET not in " ".join(c.argv) for c in host.calls)
        marker = host.home / "state" / "channel-pdf-policy-v2"
        assert marker.is_file() and stat.S_IMODE(marker.stat().st_mode) == 0o600
        # Only the analyzer's own commands are handed the Docker Hub secret.
        holders = {c.cmd for c in host.calls if HUB_SECRET in c.env.values()}
        assert holders == {"sbx"}

    def test_the_analyzer_policy_is_initialised_once(self, host: Host) -> None:
        (host.home / "state" / "channel-pdf-policy-v2").write_text("")

        assert host.run("prepare") == 0

        assert [line for line in host.lines() if line.startswith("sbx")] == [
            "sbx --app-name lantern-analysis login --username hubuser --password-stdin",
            "sbx --app-name lantern-analysis version",
        ]

    def test_an_unhappy_doctor_stops_before_the_hold(self, host: Host) -> None:
        host.script("lantern doctor", [1])

        assert host.run("prepare") != 0

        assert host.lines() == ["lantern doctor"]
        assert host.holds == set() and host.notices == []
        assert host.run("deploy") != 0
        assert host.lines() == ["lantern doctor"]
        assert host.run("finish", "--status", "failure") == 0
        assert host.lines() == ["lantern doctor"]  # no hold was taken: none is released
        assert host.modes() == ["deploy-check", "deploy-notify"]
        assert host.notices == [notice("before_install")]
        assert host.deploy_state() is None and host.leftovers() == []

    @pytest.mark.parametrize(
        "broken",
        [
            {"DOCKERHUB_USERNAME": ""},
            {"DOCKERHUB_TOKEN": None},
        ],
    )
    def test_a_missing_docker_hub_secret_fails_before_the_hold(
        self, host: Host, broken: dict[str, str | None]
    ) -> None:
        assert host.run("prepare", **broken) != 0

        assert host.lines() == ["lantern doctor"]
        assert host.holds == set()

    @pytest.mark.parametrize("step", ["login", "policy init", "policy deny", "version"])
    def test_an_analyzer_that_cannot_be_prepared_fails_before_the_hold(
        self, host: Host, step: str
    ) -> None:
        host.script(f"sbx --app-name lantern-analysis {step}", [1])

        assert host.run("prepare") != 0

        assert host.lines()[-1:] != [] and host.lines()[-1].startswith(
            f"sbx --app-name lantern-analysis {step}"
        )
        assert host.holds == set() and host.notices == []
        # The policy is not marked initialised unless both commands worked.
        marked = (host.home / "state" / "channel-pdf-policy-v2").exists()
        assert marked is (step == "version")

    def test_a_busy_daemon_is_announced_as_staged_and_waited_for(self, host: Host) -> None:
        host.statuses(BUSY, BUSY, BUSY)

        assert host.run("prepare") == 0

        busy = "run-7 \u2014 Fix the thing"
        assert host.notices == [notice("staged", CURRENT=busy)]
        assert host.slept == [15, 15]
        lines = host.lines()
        # The hold stands before the first wait, so idle stays idle.
        assert lines[5] == f"lantern daemon ctl pause --hold {HOLD} --timeout 30"
        assert lines[6:] == ["lantern daemon ctl status --json --timeout 30"] * 4
        assert host.restarts == 0

    def test_a_claim_in_progress_counts_as_busy(self, host: Host) -> None:
        host.statuses(CLAIMING, CLAIMING)

        assert host.run("prepare") == 0

        assert host.notices == [notice("staged", CURRENT="claiming o/r#12")]
        assert host.slept == [15]

    def test_an_answer_without_structured_status_fails_with_nothing_installed(
        self, host: Host, capfd: pytest.CaptureFixture[str]
    ) -> None:
        host.statuses(BUSY, BUSY, 1)

        assert host.run("prepare") != 0

        assert "gave no structured status" in capfd.readouterr().out
        assert host.slept == [15]
        assert host.holds == {HOLD}  # taken before the wait; `finish` releases it
        assert host.run("deploy") != 0
        assert host.installed == OLD and host.restarts == 0
        assert host.run("finish", "--status", "failure") == 0
        assert host.holds == set() and host.released == [HOLD]
        assert host.notices[-1:] == [notice("before_install")]

    @pytest.mark.parametrize(
        "answer", ["not json at all\n", '{"current": "run-7"}\n'], ids=["prose", "no run"]
    )
    def test_an_answer_it_cannot_read_fails_the_wait(self, host: Host, answer: str) -> None:
        """Exit 0 with something the workflow's jq program would have choked
        on: the wait fails rather than read it as idle."""
        host.statuses({}, answer)

        assert host.run("prepare") != 0
        assert host.slept == []
        assert host.holds == {HOLD} and host.installed == OLD

    def test_a_daemon_that_never_answers_has_nothing_to_drain(
        self, host: Host, capfd: pytest.CaptureFixture[str]
    ) -> None:
        host.daemon_up = False

        assert host.run("prepare") == 0

        # Twenty consecutive misses, fifteen seconds apart: five minutes.
        assert host.slept == [15] * 19
        assert "::warning::no daemon answered ctl status for 5 min" in capfd.readouterr().out
        assert host.notices == [notice("deploying")]  # "unknown" reads as not busy
        assert host.lines()[5:] == [
            f"lantern daemon ctl pause --hold {HOLD} --timeout 30",
            *["lantern daemon ctl status --json --timeout 30"] * 21,
        ]

    def test_an_answer_resets_the_count_of_misses(
        self, host: Host, capfd: pytest.CaptureFixture[str]
    ) -> None:
        host.statuses({}, *[2] * 19, BUSY, *[2] * 19)

        assert host.run("prepare") == 0

        assert host.slept == [15] * 39
        assert "::warning::" not in capfd.readouterr().out

    def test_a_chat_outage_does_not_fail_the_wait(self, host: Host) -> None:
        host.chat_down = True

        assert host.run("prepare") == 0

        assert host.modes() == ["deploy-check", "deploy-notify"]
        assert host.holds == {HOLD} and host.notices == []

    def test_the_same_run_id_again_starts_over(self, host: Host) -> None:
        """A re-run under the id of a job whose `finish` never ran."""
        host.statuses(BUSY, BUSY, 1)
        assert host.run("prepare") != 0
        assert host.holds == {HOLD} and host.leftovers() != []
        host.forget()

        assert host.run("prepare") == 0

        assert host.modes() == ["deploy-check", "deploy-notify"]
        assert host.run("deploy") == 0
        assert host.run("finish", "--status", "success") == 0
        assert host.installed == NEW
        assert host.released == [HOLD] and host.leftovers() == []


# -- deploy ------------------------------------------------------------------


class TestDeploy:
    def test_the_happy_path_end_to_end(self, host: Host) -> None:
        assert host.run("prepare") == 0
        host.forget()

        assert host.run("deploy") == 0

        assert host.modes() == FETCH
        assert host.lines() == forward_lines(host)
        assert host.slept == [45]
        assert host.installed == NEW and host.restarts == 1
        assert host.notices == []
        # The attempt is on record until `finish` says how it ended.
        assert host.deploy_state() == {
            "finished_at": 0,
            "blocked_through": NEW,
            "in_progress": True,
        }
        (line,) = host.history()
        assert re.fullmatch(rf"\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ {OLD} -> {NEW} run={RUN_ID}", line)
        assert host.holds == {HOLD}  # still ours until `finish`
        host.forget()

        started = int(host.clock())
        assert host.run("finish", "--status", "success") == 0

        assert host.modes() == ["deploy-finish", "deploy-notify"]
        assert host.lines() == release_lines()
        assert host.holds == set() and host.released == [HOLD]
        assert host.notices == [notice("restarted")]
        state = host.deploy_state()
        assert state is not None and state["in_progress"] is False
        assert state["blocked_through"] == NEW and started <= state["finished_at"] <= started + 60
        assert host.helper("deploy-finish").env["HEALTH"] == "success"
        assert host.leftovers() == []

    def test_the_wheels_are_the_release_s_own_checked_files(self, host: Host) -> None:
        host.prepared()

        assert host.run("deploy") == 0

        installs = [c for c in host.calls if c.cmd == "uv"]
        assert len(installs) == 1
        (install,) = installs
        worker, backend = (Path(arg.split("[")[0]) for arg in install.argv[-2:])
        assert install.argv[-1].endswith("[discord,slack]")
        assert worker.parent == backend.parent
        assert worker.read_bytes() == f"lantern_worker-{NEW}-py3-none-any.whl as published".encode()
        assert backend.read_bytes() == (
            f"lantern_backend-{NEW}-py3-none-any.whl as published".encode()
        )
        for mode in ("download-wheels", "verify-download"):
            assert host.helper(mode).env["VERSION"] == NEW
            assert host.helper(mode).env["DIST"] == str(worker.parent)
        assert install.env["UV_CACHE_DIR"] == f"{host.home}/cache/uv"

    def test_the_report_says_how_long_the_daemon_was_waited_for(self, host: Host) -> None:
        host.statuses(BUSY, BUSY, BUSY, BUSY, BUSY)

        assert host.run("prepare") == 0
        assert host.slept == [15] * 4
        assert host.run("deploy") == 0
        assert host.run("finish", "--status", "success") == 0

        waited = " after waiting 1 min for the daemon to go idle"
        assert host.notices[-1:] == [notice("restarted", waited=waited)]

    def test_a_release_published_during_the_wait_is_the_one_installed(self, host: Host) -> None:
        host.statuses(BUSY, BUSY)
        host.during_first_sleep(lambda: host.publish(NEWER))

        assert host.run("prepare") == 0
        assert host.notices == [notice("staged", CURRENT="run-7 \u2014 Fix the thing")]
        host.forget()
        assert host.run("deploy") == 0

        assert host.modes() == ["deploy-check", "deploy-notify", *FETCH[1:]]
        assert host.notices == [notice("refreshed", VERSION=NEWER)]
        assert host.lines() == forward_lines(host, NEWER)
        assert host.installed == NEWER
        assert host.helper("deploy-start").env["VERSION"] == NEWER
        assert [line.split(" ", 1)[1] for line in host.history()] == [
            f"{OLD} -> {NEWER} run={RUN_ID}"
        ]
        assert host.run("finish", "--status", "success") == 0
        # Fifteen seconds were waited, and the workflow counts whole minutes.
        waited = " after waiting 0 min for the daemon to go idle"
        assert host.notices[-1:] == [notice("restarted", VERSION=NEWER, waited=waited)]
        state = host.deploy_state()
        assert state is not None and state["blocked_through"] == NEWER

    def test_a_host_brought_current_during_the_wait_is_left_alone(self, host: Host) -> None:
        host.statuses(BUSY, BUSY)
        host.on_sleep.append(lambda seconds: setattr(host, "installed", NEW))
        assert host.run("prepare") == 0
        host.forget()

        assert host.run("deploy") == 0

        assert host.modes() == ["deploy-check"]
        assert host.lines() == [] and host.restarts == 0
        assert host.run("finish", "--status", "success") == 0
        assert host.notices == [notice("no_upgrade", DECISION="current")]
        assert host.released == [HOLD]
        assert host.deploy_state() is None and host.history() == []

    def test_a_refresh_that_cannot_decide_fails_with_nothing_installed(self, host: Host) -> None:
        host.prepared()
        host.github.release_api_error = OSError("unavailable")

        assert host.run("deploy") != 0

        assert host.modes() == ["deploy-check"]
        assert host.lines() == []
        host.github.release_api_error = None
        assert host.run("finish", "--status", "failure") == 0
        assert host.modes()[1:] == ["deploy-notify"]  # no outcome: no attempt was recorded
        assert host.notices == [notice("before_install")]
        assert host.released == [HOLD] and host.deploy_state() is None

    def test_wheels_not_downloadable_yet_are_retried(self, host: Host) -> None:
        host.prepared()
        host.download_failures = 2

        assert host.run("deploy") == 0

        assert host.modes() == ["deploy-check", *["download-wheels"] * 3, *FETCH[2:]]
        assert host.slept == [15, 15, 45]
        assert host.installed == NEW

    def test_wheels_that_never_download_fail_before_anything_is_installed(
        self, host: Host, capfd: pytest.CaptureFixture[str]
    ) -> None:
        host.prepared()
        host.unavailable.add(NEW)

        assert host.run("deploy") != 0

        assert host.modes() == ["deploy-check", *["download-wheels"] * 5]
        assert host.slept == [15] * 5
        assert f"::error::could not download the wheels for v{NEW}" in capfd.readouterr().out
        # Nothing was installed, so nothing is rolled back or restarted.
        assert host.lines() == [] and host.restarts == 0
        assert host.deploy_state() is None and host.history() == []
        assert host.run("finish", "--status", "failure") == 0
        assert host.modes()[-1:] == ["deploy-notify"] and "deploy-finish" not in host.modes()
        assert host.notices == [notice("before_install")]
        assert host.released == [HOLD] and host.leftovers() == []

    def test_wheels_that_differ_from_the_manifest_are_not_retried(self, host: Host) -> None:
        host.prepared()
        host.break_manifest(NEW)

        assert host.run("deploy") != 0

        assert host.modes() == ["deploy-check", "download-wheels", "verify-download"]
        assert host.lines() == [] and host.slept == []
        assert host.deploy_state() is None

    def test_an_attempt_that_cannot_be_recorded_stops_before_the_upgrade(self, host: Host) -> None:
        host.prepared()
        host.script("python deploy-start", [1])

        assert host.run("deploy") != 0

        assert host.modes() == FETCH
        assert host.lines() == []
        assert host.run("finish", "--status", "failure") == 0
        assert "deploy-finish" not in host.modes()
        assert host.notices == [notice("before_install")]

    def test_a_host_that_has_baked_gets_its_template_refreshed(self, host: Host) -> None:
        (host.home / "state" / "bake.json").write_text("{}")
        host.prepared()

        assert host.run("deploy") == 0

        assert host.lines() == [*forward_lines(host), "lantern bake"]
        bake = host.calls[-1]
        assert bake.line == "lantern bake" and bake.stdin_devnull

    def test_a_bake_that_fails_does_not_fail_a_healthy_deploy(self, host: Host) -> None:
        (host.home / "state" / "bake.json").write_text("{}")
        host.prepared()
        host.script("lantern bake", [1])

        assert host.run("deploy") == 0

        assert host.lines()[-1:] == ["lantern bake"]
        assert host.restarts == 1  # no rollback
        assert host.run("finish", "--status", "success") == 0
        assert host.notices == [notice("restarted")]

    def test_a_second_deploy_in_the_same_job_is_refused(self, host: Host) -> None:
        host.prepared()
        assert host.run("deploy") == 0
        assert host.restarts == 1
        host.forget()

        assert host.run("deploy") != 0

        # Nothing is fetched, installed or restarted a second time.
        assert host.calls == [] and host.restarts == 1


# -- failure and rollback ----------------------------------------------------

#: Each failure from the upgrade on: how to cause it, how many of the
#: forward commands ran (the failing one included), and what was slept.
FAILURES: dict[str, tuple[Any, int, list[int]]] = {
    "backup": (lambda h: h.script("lantern backup", [1]), 1, [15]),
    "pip install": (lambda h: h.script("uv pip install", [1]), 2, [15]),
    "init": (lambda h: h.script("lantern init", [1]), 4, [15]),
    "restart": (lambda h: h.script("systemctl --user restart", [1]), 6, [15]),
    "unit not active": (lambda h: h.script("systemctl --user is-active", [3]), 7, [15]),
    "version mismatch": (lambda h: h.pip_noops.add(1), 8, [15]),
    "doctor": (lambda h: h.script("lantern doctor", [1]), 9, [15]),
    "no control answer": (
        lambda h: h.script("lantern daemon ctl status --json --timeout 300", [2]),
        10,
        [15],
    ),
    "restart count unreadable": (lambda h: h.script("systemctl --user show", [1]), 11, [15]),
    "died while settling": (
        lambda h: h.script("systemctl --user is-active", [None, 3]),
        12,
        [45, 15],
    ),
    "crash loop": (
        lambda h: h.script("systemctl --user show", [(0, "0\n"), (0, "1\n")]),
        13,
        [45, 15],
    ),
}

#: A rollback that does not restore the host: how to cause it (after a
#: failed restart), which rollback commands still ran, what was slept, and
#: whether the workflow's last-ditch line is printed.
BROKEN_ROLLBACKS: dict[str, tuple[Any, int, list[int], bool]] = {
    "previous wheels unavailable": (lambda h: h.unavailable.add(OLD), 0, [15, 15], False),
    "previous wheels fail the manifest": (lambda h: h.break_manifest(OLD), 0, [], False),
    "pip install": (lambda h: h.script("uv pip install", [None, 1]), 1, [], False),
    "restart": (lambda h: h.script("systemctl --user restart", [1]), 4, [], False),
    "unit not active": (lambda h: h.script("systemctl --user is-active", [3]), 5, [15], True),
    "wrong version": (lambda h: h.pip_noops.add(2), 6, [15], True),
    "doctor": (lambda h: h.script("lantern doctor", [1]), 7, [15], True),
    "no control answer": (
        lambda h: h.script("lantern daemon ctl status --json --timeout 300", [2]),
        8,
        [15],
        True,
    ),
}


class TestRollback:
    @pytest.mark.parametrize("failure", FAILURES)
    def test_a_failure_from_the_upgrade_on_rolls_back(
        self, host: Host, capfd: pytest.CaptureFixture[str], failure: str
    ) -> None:
        cause, ran, slept = FAILURES[failure]
        host.prepared()
        cause(host)

        assert host.run("deploy") != 0

        assert host.lines() == [*forward_lines(host)[:ran], *rollback_lines(host)]
        assert host.slept == slept
        assert host.modes() == [*FETCH, "download-wheels", "verify-download"]
        for mode in ("download-wheels", "verify-download"):
            assert host.helper(mode, 1).env["VERSION"] == OLD
        assert host.installed == OLD
        assert f"rolling back to {OLD}" in capfd.readouterr().out
        # The outcome is `finish`'s to record: until then the attempt stands.
        state = host.deploy_state()
        assert state is not None and state["in_progress"] is True
        host.forget()

        assert host.run("finish", "--status", "failure") == 0

        assert host.modes() == ["deploy-finish", "deploy-notify"]
        assert host.helper("deploy-finish").env["RESTORED"] == "true"
        state = host.deploy_state()
        assert state is not None and state["in_progress"] is False
        assert state["blocked_through"] == NEW  # not retried automatically
        assert host.notices == [notice("rolled_back")]
        assert host.lines() == release_lines() and host.holds == set()
        assert host.leftovers() == []

    @pytest.mark.parametrize("broken", BROKEN_ROLLBACKS)
    def test_a_rollback_that_cannot_restore_health_needs_a_human(
        self, host: Host, capfd: pytest.CaptureFixture[str], broken: str
    ) -> None:
        cause, ran, slept, last_ditch = BROKEN_ROLLBACKS[broken]
        host.prepared()
        host.script("systemctl --user restart", [1])
        cause(host)

        assert host.run("deploy") != 0

        assert host.lines() == [*forward_lines(host)[:6], *rollback_lines(host)[:ran]]
        assert host.slept == slept
        said = "ROLLBACK ALSO FAILED \u2014 test-host needs a human" in capfd.readouterr().out
        assert said is last_ditch
        host.forget()

        assert host.run("finish", "--status", "failure") == 0

        assert host.helper("deploy-finish").env["RESTORED"] != "true"
        state = host.deploy_state()
        # Automatic deploys stay blocked until someone recovers the host.
        assert state is not None and state["in_progress"] is True
        assert host.notices == [notice("needs_human")]
        assert host.released == [HOLD] and host.leftovers() == []

    def test_previous_wheels_are_tried_three_times(self, host: Host) -> None:
        host.prepared()
        host.script("systemctl --user restart", [1])
        host.unavailable.add(OLD)

        assert host.run("deploy") != 0

        assert host.modes() == [*FETCH, *["download-wheels"] * 3]
        assert "uv pip install" not in " ".join(host.lines()[6:])

    def test_a_rollback_survives_an_init_that_fails(self, host: Host) -> None:
        host.prepared()
        host.script("systemctl --user restart", [1])
        host.script("lantern init", [None, 1])

        assert host.run("deploy") != 0

        assert host.lines() == [*forward_lines(host)[:6], *rollback_lines(host)]
        assert host.run("finish", "--status", "failure") == 0
        assert host.notices == [notice("rolled_back")]

    def test_a_rollback_does_not_refresh_the_baked_template(self, host: Host) -> None:
        (host.home / "state" / "bake.json").write_text("{}")
        host.prepared()
        host.script("lantern doctor", [1])

        assert host.run("deploy") != 0

        assert "lantern bake" not in host.lines()


# -- finish ------------------------------------------------------------------


class TestFinish:
    @pytest.mark.parametrize("status", ["success", "failure", "timed_out", "cancelled"])
    def test_after_nothing_ran_it_does_nothing(self, host: Host, status: str) -> None:
        assert host.run("finish", "--status", status) == 0
        assert host.calls == [] and host.leftovers() == []

    def test_it_needs_nothing_from_the_environment(self, host: Host) -> None:
        assert deploy_daemon.main(["finish"], env={}, sleep=host.sleep, clock=host.clock) == 0
        assert host.calls == []

    def test_a_timeout_before_the_restart_releases_the_hold(self, host: Host) -> None:
        host.prepared()

        assert host.run("finish", "--status", "timed_out") == 0

        # `job.status` has no word for a timeout: the runner cancelled the job.
        assert host.notices == [notice("before_install", RESULT="cancelled")]
        assert host.lines() == release_lines()
        assert host.modes() == ["deploy-notify"]
        assert host.installed == OLD and host.restarts == 0 and host.deploy_state() is None

    def test_the_status_may_come_from_the_environment(self, host: Host) -> None:
        host.prepared()

        assert host.run("finish", DEPLOY_JOB_STATUS="cancelled") == 0

        assert host.notices == [notice("before_install", RESULT="cancelled")]

    @pytest.mark.parametrize("status", [None, "", "skipped"])
    def test_a_status_it_does_not_know_is_not_reported_as_success(
        self, host: Host, status: str | None
    ) -> None:
        assert host.run("prepare") == 0
        assert host.run("deploy") == 0
        host.forget()

        assert host.run("finish", *([] if status is None else ["--status", status])) == 0

        assert host.released == [HOLD]
        (posted,) = host.notices
        assert "**failure**" in posted[1] and "\u2705" not in posted[1]

    def test_a_second_finish_does_nothing(self, host: Host) -> None:
        assert host.run("prepare") == 0
        assert host.run("deploy") == 0
        assert host.run("finish", "--status", "success") == 0
        assert host.released == [HOLD]
        state = host.deploy_state()
        host.forget()

        assert host.run("finish", "--status", "success") == 0

        assert host.calls == [] and host.deploy_state() == state
        assert host.released == [HOLD]

    def test_a_hold_that_cannot_be_released_is_a_warning(
        self, host: Host, capfd: pytest.CaptureFixture[str]
    ) -> None:
        host.prepared()
        host.script("lantern daemon ctl resume", [2])

        assert host.run("finish", "--status", "failure") == 0

        assert f"::warning::could not release hold {HOLD}" in capfd.readouterr().out
        assert host.lines() == release_lines()
        assert host.notices == [notice("before_install")]

    def test_a_chat_outage_does_not_fail_the_report(self, host: Host) -> None:
        assert host.run("prepare") == 0
        assert host.run("deploy") == 0
        host.chat_down = True

        assert host.run("finish", "--status", "success") == 0

        assert host.released == [HOLD] and host.leftovers() == []
        state = host.deploy_state()
        assert state is not None and state["in_progress"] is False

    def test_an_outcome_that_cannot_be_recorded_fails_the_job(self, host: Host) -> None:
        assert host.run("prepare") == 0
        assert host.run("deploy") == 0
        host.forget()
        host.script("python deploy-finish", [1])

        assert host.run("finish", "--status", "success") != 0

        # The rest of `finish` still happens, and the report follows the job.
        assert host.released == [HOLD] and host.leftovers() == []
        (posted,) = host.notices
        assert "**failure**" in posted[1]

    def test_a_job_that_died_before_its_first_record_leaves_nothing(self, host: Host) -> None:
        host.installed = NEW
        assert host.run("prepare") == 0
        assert len(host.leftovers()) == 1
        (entry,) = host.runner_temp.iterdir()
        for recorded in entry.iterdir():
            recorded.unlink()
        host.forget()

        assert host.run("finish", "--status", "cancelled") == 0

        assert host.calls == [] and host.leftovers() == []

    def test_a_state_directory_others_could_write_is_not_trusted(self, host: Host) -> None:
        host.prepared()
        assert len(host.leftovers()) == 1
        (entry,) = host.runner_temp.iterdir()
        assert stat.S_IMODE(entry.stat().st_mode) == 0o700
        for path in entry.rglob("*.json"):
            path.write_text(path.read_text().replace(HOLD, "operator"))
        entry.chmod(0o777)

        assert host.run("finish", "--status", "failure") != 0

        assert host.calls == [] and host.holds == {HOLD}


class TestKilled:
    """The runner's own process dies between two commands. `finish` runs
    afterwards and must leave the daemon as the workflow would."""

    def test_killed_while_waiting_for_idle_releases_the_hold(self, host: Host) -> None:
        # The hold step's own status answers; the wait's first one never does.
        host.block("lantern daemon ctl status --json", skip=1)
        process = host.spawn("prepare")
        host.kill_when_blocked(process)
        assert host.holds == {HOLD}
        host.forget()

        assert host.run("finish", "--status", "cancelled") == 0

        assert host.lines() == release_lines()
        assert host.holds == set()
        assert host.modes() == ["deploy-notify"]  # no attempt was recorded: no outcome
        assert host.notices == [notice("before_install", RESULT="cancelled")]
        assert host.deploy_state() is None and host.leftovers() == []

    def test_killed_inside_the_hold_step_still_releases_what_was_asked_for(
        self, host: Host
    ) -> None:
        host.block("lantern daemon ctl pause")
        process = host.spawn("prepare")
        host.kill_when_blocked(process)
        assert host.holds == {HOLD}  # the daemon took it; the script never heard
        host.forget()

        assert host.run("finish", "--status", "cancelled") == 0

        assert host.released == [HOLD] and host.holds == set()

    def test_killed_before_the_hold_releases_nothing(self, host: Host) -> None:
        host.block("lantern doctor")
        process = host.spawn("prepare")
        host.kill_when_blocked(process)
        host.forget()

        assert host.run("finish", "--status", "cancelled") == 0

        assert host.lines() == [] and host.released == []
        assert host.notices == [notice("before_install", RESULT="cancelled")]
        assert host.leftovers() == []

    def test_killed_mid_upgrade_leaves_the_deployment_marked_interrupted(self, host: Host) -> None:
        host.prepared()
        host.block("uv pip install")
        process = host.spawn("deploy")
        host.kill_when_blocked(process)
        host.forget()

        assert host.run("finish", "--status", "cancelled") == 0

        # No rollback ran (the job was cancelled, not failed), so the host
        # may be half-upgraded: automatic deploys stay blocked.
        assert host.modes() == ["deploy-finish", "deploy-notify"]
        assert host.helper("deploy-finish").env["HEALTH"] != "success"
        assert host.helper("deploy-finish").env["RESTORED"] != "true"
        state = host.deploy_state()
        assert state is not None and state["in_progress"] is True
        assert host.lines() == release_lines() and host.holds == set()
        assert host.restarts == 0
        assert host.notices == [notice("needs_human", RESULT="cancelled")]
        assert host.leftovers() == []

    def test_killed_between_recording_the_attempt_and_upgrading(self, host: Host) -> None:
        host.prepared()
        host.block("lantern backup")
        process = host.spawn("deploy")
        host.kill_when_blocked(process)
        host.forget()

        assert host.run("finish", "--status", "cancelled") == 0

        # The upgrade step had started, so the workflow records an outcome.
        assert host.modes() == ["deploy-finish", "deploy-notify"]
        assert host.released == [HOLD]


# -- the wanted differences --------------------------------------------------


class TestCooldownWait:
    """D-A: with `--wait-cooldown`, `prepare` outlasts the cooldown and
    decides again; without it the cooldown ends the job, as it always did."""

    def test_it_waits_out_the_cooldown_then_deploys(self, host: Host) -> None:
        host.seed_deploy_state(finished_at=int(time.time()) - 1700)
        held_while_waiting: list[set[str]] = []
        host.on_sleep.append(lambda seconds: held_while_waiting.append(set(host.holds)))

        assert host.run("prepare", "--wait-cooldown") == 0

        # A hundred seconds of cooldown were left, less however long the
        # decision took: the wait is for that, not a fixed interval.
        assert len(host.slept) == 1 and 60 <= host.slept[0] <= 100
        assert held_while_waiting == [set()]  # no hold is taken during the cooldown
        assert host.modes() == ["deploy-check", "deploy-check", "deploy-notify"]
        assert host.lines() == prepare_lines()
        assert host.run("deploy") == 0
        assert host.installed == NEW

    def test_it_goes_on_with_what_it_finds_after_the_wait(self, host: Host) -> None:
        host.seed_deploy_state(finished_at=int(time.time()) - 1000)
        host.during_first_sleep(lambda: host.publish(NEWER))

        assert host.run("prepare", "--wait-cooldown") == 0

        assert host.notices == [notice("deploying", VERSION=NEWER)]
        assert host.run("deploy") == 0
        assert host.installed == NEWER

    def test_a_host_brought_current_during_the_cooldown_is_left_alone(self, host: Host) -> None:
        host.seed_deploy_state(finished_at=int(time.time()) - 1000)
        host.on_sleep.append(lambda seconds: setattr(host, "installed", NEW))

        assert host.run("prepare", "--wait-cooldown") == 0

        assert host.modes() == ["deploy-check", "deploy-check"]
        assert host.lines() == [] and host.holds == set() and host.notices == []

    def test_without_the_flag_the_cooldown_ends_the_job(self, host: Host) -> None:
        host.seed_deploy_state(finished_at=int(time.time()) - 1000)

        assert host.run("prepare") == 0

        assert host.modes() == ["deploy-check"] and host.slept == [] and host.lines() == []

    def test_only_a_cooldown_is_waited_for(self, host: Host) -> None:
        host.seed_deploy_state(finished_at=int(time.time()) - 1000, blocked_through=NEW)

        assert host.run("prepare", "--wait-cooldown") == 0

        assert host.modes() == ["deploy-check"] and host.slept == [] and host.lines() == []

    def test_a_manual_run_never_waits(self, host: Host) -> None:
        host.seed_deploy_state(finished_at=int(time.time()) - 1000)

        assert host.run("prepare", "--wait-cooldown", GITHUB_EVENT_NAME="workflow_dispatch") == 0

        assert host.slept == []
        assert host.modes() == ["deploy-check", "deploy-notify"]
        assert host.holds == {HOLD}


def _ran_by_the_script(host: Host) -> list[Call]:
    return [c for c in host.calls if c.cmd in HOST_COMMANDS and not c.via_helper]


class TestToken:
    """D-C: the job's token is for GitHub. It reaches the release helper and
    `gh` as GH_TOKEN; no command that acts on the host ever sees it."""

    def _whole_job(self, host: Host, *argv: str, **env: str | None) -> None:
        assert host.run("prepare", *argv, **env) == 0
        assert host.run("deploy", *argv, **env) == 0
        assert host.run("finish", "--status", "success", *argv, **env) == 0

    def _no_host_command_saw(self, host: Host, *names: str) -> None:
        ran = _ran_by_the_script(host)
        assert {c.cmd for c in ran} == set(HOST_COMMANDS)
        for call in ran:
            assert not [name for name in names if name in call.env], call.line
            assert TOKEN not in call.env.values(), call.line

    def test_the_token_reaches_github_and_never_the_host(self, host: Host) -> None:
        self._whole_job(host, GH_TOKEN="ambient-gh-token", GITHUB_TOKEN="ambient-github-token")

        self._no_host_command_saw(host, *TOKEN_NAMES)
        asked = [c for c in host.calls if c.cmd == "gh"]
        assert asked and all(c.env.get("GH_TOKEN") == TOKEN for c in asked)
        for call in (c for c in host.calls if c.cmd in ("python", "notify", "gh")):
            # Under one name only, and only where the workflow gave it.
            assert "GITHUB_TOKEN" not in call.env, call.line
            assert "DEPLOY_GITHUB_TOKEN" not in call.env, call.line
            if call.cmd == "python":
                expected = TOKEN if call.argv[1] in TOKEN_MODES else None
                assert call.env.get("GH_TOKEN") == expected, call.line
            if call.cmd == "notify":
                assert "GH_TOKEN" not in call.env

    def test_a_rollback_keeps_the_same_rule(self, host: Host) -> None:
        host.prepared(GH_TOKEN="ambient-gh-token")
        host.script("lantern doctor", [1])

        assert host.run("deploy", GH_TOKEN="ambient-gh-token") != 0

        assert host.lines()[-len(rollback_lines(host)) :] == rollback_lines(host)
        for call in _ran_by_the_script(host):
            assert not [name for name in TOKEN_NAMES if name in call.env], call.line
        for index in (0, 1):
            assert host.helper("download-wheels", index).env["GH_TOKEN"] == TOKEN
            assert host.helper("verify-download", index).env["GH_TOKEN"] == TOKEN

    def test_gh_token_is_the_fallback(self, host: Host) -> None:
        self._whole_job(host, DEPLOY_GITHUB_TOKEN=None, GH_TOKEN=TOKEN)

        self._no_host_command_saw(host, *TOKEN_NAMES)
        asked = [c for c in host.calls if c.cmd == "gh"]
        assert asked and all(c.env.get("GH_TOKEN") == TOKEN for c in asked)

    @pytest.mark.parametrize("how", ["option", "environment"])
    def test_the_variable_s_name_is_configurable(self, host: Host, how: str) -> None:
        """A runner that keeps its token under a name of its own points the
        script at it. That variable is the one the helper is given, and it,
        the default name and both GitHub names stay away from the host."""
        argv = ["--token-env", "RUNNER_JOB_TOKEN"] if how == "option" else []
        env: dict[str, str | None] = {
            "RUNNER_JOB_TOKEN": TOKEN,
            "DEPLOY_GITHUB_TOKEN": "a-token-under-the-default-name",
            "GH_TOKEN": "ambient-gh-token",
            "GITHUB_TOKEN": "ambient-github-token",
        }
        if how == "environment":
            env["DEPLOY_TOKEN_ENV"] = "RUNNER_JOB_TOKEN"

        self._whole_job(host, *argv, **env)

        self._no_host_command_saw(host, *TOKEN_NAMES, "RUNNER_JOB_TOKEN")
        asked = [c for c in host.calls if c.cmd == "gh"]
        assert asked and all(c.env.get("GH_TOKEN") == TOKEN for c in asked)
        for call in (c for c in host.calls if c.cmd in ("python", "notify", "gh")):
            # The helper is handed the configured variable's value, as
            # GH_TOKEN and nothing else; the notifier is handed none.
            for name in ("RUNNER_JOB_TOKEN", "DEPLOY_GITHUB_TOKEN", "GITHUB_TOKEN"):
                assert name not in call.env, (call.line, name)
            if call.cmd == "python":
                expected = TOKEN if call.argv[1] in TOKEN_MODES else None
                assert call.env.get("GH_TOKEN") == expected, call.line
            if call.cmd == "notify":
                assert "GH_TOKEN" not in call.env


class TestHoldName:
    """D-D: `deploy-<run id>`, with a prefix when two runners share a host."""

    def test_the_default_is_the_workflow_s(self, host: Host) -> None:
        assert host.run("prepare") == 0
        assert host.holds == {f"deploy-{RUN_ID}"}

    @pytest.mark.parametrize("how", ["option", "environment"])
    def test_a_prefix_keeps_two_runners_apart(self, host: Host, how: str) -> None:
        if how == "option":
            assert host.run("prepare", "--hold-prefix", "second-") == 0
        else:
            assert host.run("prepare", DEPLOY_HOLD_PREFIX="second-") == 0

        name = f"second-deploy-{RUN_ID}"
        assert host.holds == {name}
        assert hold_name(name) == name  # the daemon's own rule
        assert host.run("deploy") == 0
        # `finish` is told nothing: it releases the name `prepare` recorded.
        assert host.run("finish", "--status", "success") == 0
        assert host.released == [name] and host.holds == set()
        # The history line carries the run id as given, without the prefix.
        assert [line.split(" ", 1)[1] for line in host.history()] == [
            f"{OLD} -> {NEW} run={RUN_ID}"
        ]

    def test_a_busy_notice_names_the_prefixed_hold(self, host: Host) -> None:
        host.statuses(BUSY)

        assert host.run("prepare", "--hold-prefix", "second-") == 0

        assert host.notices == [
            notice("staged", CURRENT="run-7 \u2014 Fix the thing", HOLD=f"second-deploy-{RUN_ID}")
        ]

    @pytest.mark.parametrize(
        "env",
        [
            {"DEPLOY_HOLD_PREFIX": "two words-"},
            {"DEPLOY_HOLD_PREFIX": "-leading-dash-"},
            {"DEPLOY_HOLD_PREFIX": "p" * 60},
            {"GITHUB_RUN_ID": "12/../34"},
            {"GITHUB_RUN_ID": ""},
        ],
    )
    def test_a_name_the_daemon_would_refuse_stops_before_anything_is_touched(
        self, host: Host, env: dict[str, str]
    ) -> None:
        assert host.run("prepare", **env) != 0
        assert host.calls == [] and host.holds == set()


class TestEvents:
    """D-E: manual or automatic comes from the event name; everything else
    the workflow took from its context arrives in the environment."""

    @pytest.mark.parametrize(
        ("event", "manual"),
        [
            ("workflow_dispatch", "true"),
            ("release", "false"),
            ("schedule", "false"),
            (None, "false"),
        ],
    )
    def test_only_a_dispatch_is_manual(self, host: Host, event: str | None, manual: str) -> None:
        # A stray MANUAL in the job's environment decides nothing.
        opposite = "false" if manual == "true" else "true"
        host.seed_deploy_state(finished_at=int(time.time()) - 60)

        assert host.run("prepare", GITHUB_EVENT_NAME=event, MANUAL=opposite) == 0

        assert host.modes()[:1] == ["deploy-check"]
        assert host.helper("deploy-check").env["MANUAL"] == manual
        # Manual deploys bypass the cooldown; automatic ones do not.
        assert (host.holds == {HOLD}) is (manual == "true")

    def test_a_manual_version_stays_pinned_through_the_wait(self, host: Host) -> None:
        host.statuses(BUSY, BUSY)
        host.during_first_sleep(lambda: host.publish(NEWER))
        manual = {"GITHUB_EVENT_NAME": "workflow_dispatch", "INPUT_VERSION": NEW}

        assert host.run("prepare", **manual) == 0
        assert host.run("deploy", **manual) == 0

        assert host.installed == NEW
        assert all(text != notice("refreshed", VERSION=NEWER)[1] for _, text in host.notices)
        # A manual deploy suppresses automatic ones through the newest release.
        state = host.deploy_state()
        assert state is not None and state["blocked_through"] == NEWER

    def test_a_manual_deploy_can_reinstall_after_an_interruption(self, host: Host) -> None:
        host.installed = NEW
        host.seed_deploy_state(in_progress=True, blocked_through=NEW)
        manual = {"GITHUB_EVENT_NAME": "workflow_dispatch", "INPUT_VERSION": f"v{NEW}"}

        assert host.run("prepare", **manual) == 0
        assert host.run("deploy", **manual) == 0
        assert host.run("finish", "--status", "success", **manual) == 0

        assert host.restarts == 1
        state = host.deploy_state()
        assert state is not None and state["in_progress"] is False
        assert host.notices[-1:] == [notice("restarted", FROM=NEW)]

    @pytest.mark.parametrize("missing", ["HOST", "DEPLOY_CHANNEL", "GITHUB_REPOSITORY"])
    def test_a_setting_the_workflow_always_had_is_required(self, host: Host, missing: str) -> None:
        assert host.run("prepare", **{missing: None}) != 0
        assert host.run("prepare", **{missing: ""}) != 0
        assert host.calls == []

    def test_the_unit_defaults_to_the_one_init_renders(self, host: Host) -> None:
        host.prepared(UNIT=None)

        assert host.run("deploy", UNIT=None) == 0

        assert "systemctl --user restart lantern-daemon" in host.lines()

    def test_the_run_link_can_be_composed_as_github_does(self, host: Host) -> None:
        github = {"RUN_URL": None, "GITHUB_SERVER_URL": "https://github.example.test"}

        assert host.run("prepare", **github) == 0

        link = f"https://github.example.test/o/r/actions/runs/{RUN_ID}"
        assert host.notices == [notice("deploying", RUN_URL=link)]

    def test_a_job_with_no_run_link_stops_before_anything_is_touched(self, host: Host) -> None:
        assert host.run("prepare", RUN_URL=None) != 0
        assert host.calls == []


# -- the contract's own wording ----------------------------------------------


@pytest.mark.parametrize("kind", NOTICES)
def test_the_notices_are_the_workflow_s_word_for_word(kind: str) -> None:
    """The expectations above are the workflow's templates, not a paraphrase."""
    as_bash = re.sub(r"\{(\w+)\}", r"${\1}", NOTICES[kind]).replace("`", "\\`")
    assert as_bash in WORKFLOW.read_text(encoding="utf-8")


def test_the_script_is_plain_ascii_and_stdlib_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.isascii()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"release_pipeline"}


# -- as the runner starts it -------------------------------------------------


class TestCommandLine:
    def _finish(self, process: subprocess.Popen[str]) -> tuple[int, str]:
        output, _ = process.communicate(timeout=120)
        return process.returncode, output

    def test_three_steps_as_separate_processes(self, host: Host) -> None:
        code, output = self._finish(host.spawn("prepare"))
        assert code == 0, output
        assert host.holds == {HOLD}
        # `deploy` really sleeps its 45 s settle here, so stop at the decision.
        host.installed = NEW
        code, output = self._finish(host.spawn("deploy"))
        assert code == 0, output
        code, output = self._finish(host.spawn("finish", "--status", "success"))
        assert code == 0, output
        assert host.released == [HOLD] and host.leftovers() == []
        assert host.notices[-1:] == [notice("no_upgrade", DECISION="current")]

    def test_a_failure_is_a_non_zero_exit(self, host: Host) -> None:
        host.script("lantern doctor", [1])
        code, output = self._finish(host.spawn("prepare"))
        assert code == 1, output

    def test_an_entry_point_it_does_not_have_is_refused(self, host: Host) -> None:
        code, _ = self._finish(host.spawn("rollback"))
        assert code == 2
        assert host.calls == []
