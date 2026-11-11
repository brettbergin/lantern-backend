"""lantern's own deploy, as three entry points for a runner without step conditions.

`.github/workflows/deploy.yml` is this pipeline as one GitHub Actions job: its
`if:` conditions, its `always()` steps and its step outputs carry the logic. A
runner that only runs a list of steps in order, stops at the first failure and
then runs a list of "finally" steps cannot express any of that, so the same
steps live here, under the same conditions and in the same order:

    prepare   "Resolve host paths" through "Wait for the daemon to go idle".
              Ends successfully, having changed nothing, when there is nothing
              to deploy.
    deploy    "Refresh the target after draining" through "Refresh the baked
              template", and "Roll back" when anything from the upgrade on
              fails. Exits non-zero when the deploy failed, restored or not.
    finish    The workflow's always() steps: record the outcome, release the
              hold this job took, remove what it downloaded, report. Safe to
              run whatever the two before it did, or did not get to do.

The three share one file, `state.json`, in a directory of this job's own under
RUNNER_TEMP. It holds what `steps.<id>.outcome` and `steps.<id>.outputs` held
on GitHub and is rewritten, atomically, before and after every step, so a job
killed at any point leaves a record `finish` can act on.

What differs from the workflow, on purpose: there is no release receipt to
read; `prepare --wait-cooldown` outlasts a cooldown instead of leaving it to
a schedule; the hold's name may carry a prefix; and the job's GitHub token,
which this kind of runner hands every step under one name, is passed on only
to the release helper (as GH_TOKEN) and removed from the environment of every
command that acts on the host, which must use the host's own credentials.

Stdlib only, like `release_pipeline.py`, and written for Python 3.10 or newer.
What that helper already does is not repeated here: its subcommands run, as in
the workflow, under the home's interpreter, from the copy beside this file.
Both files must come from a trusted revision of main, never from a pull
request or a release asset.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

PIPELINE = Path(__file__).resolve().parent / "release_pipeline.py"

#: Where the job's GitHub token is looked for first; GH_TOKEN is the fallback.
#: A runner that keeps its token elsewhere names that variable instead
#: (`--token-env`, DEPLOY_TOKEN_ENV); both names are then withheld.
DEFAULT_TOKEN_ENV = "DEPLOY_GITHUB_TOKEN"
#: The unit `lantern init --systemd` renders.
DEFAULT_UNIT = "lantern-daemon"
#: The separate sbx app the PDF analyzer runs under.
ANALYSIS_APP = "lantern-analysis"
#: Docker Hub credentials are the analyzer step's alone, as in the workflow.
DOCKERHUB = ("DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN")

#: The rule in lantern/daemon/holds.py: what the daemon accepts as a hold.
HOLD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: A run id names a directory and part of the hold: one plain token.
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: What `job.status` distinguishes: it has three words, so a job the runner
#: stopped for running out of time is one the runner cancelled. Anything
#: unrecognised is never read as success.
JOB_STATUS = {
    "success": "success",
    "failure": "failure",
    "cancelled": "cancelled",
    "timed_out": "cancelled",
}

# The notices and log lines are the workflow's, character for character.
DASH = "\u2014"
ARROW = "\u2192"
ROCKET = "\U0001f680"
CHECK = "\u2705"
CROSS = "\u274c"

Sleep = Callable[[float], None]
Clock = Callable[[], float]


class StepFailed(Exception):
    """A step failed: the steps after it are skipped, as on GitHub."""


def say(text: str) -> None:
    print(text, flush=True)


# -- the job's own state -------------------------------------------------------


class State:
    """Step outcomes and outputs, where the three entry points find them."""

    def __init__(self, directory: Path, data: dict[str, Any]) -> None:
        self.directory = directory
        self.data = data

    @property
    def settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = self.data["settings"]
        return settings

    def save(self) -> None:
        temporary = self.directory / "state.json.tmp"
        temporary.write_text(json.dumps(self.data, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.directory / "state.json")

    def begin(self, step: str) -> None:
        self.data["steps"][step] = "running"
        self.save()

    def end(self, step: str, outcome: str) -> None:
        self.data["steps"][step] = outcome
        self.save()

    def outcome(self, step: str) -> str:
        """`steps.<step>.outcome`. A step the job died in is "cancelled"."""
        outcome: str = self.data["steps"].get(step, "skipped")
        return "cancelled" if outcome == "running" else outcome

    def record(self, step: str, **outputs: str) -> None:
        self.data["outputs"].setdefault(step, {}).update(outputs)
        self.save()

    def output(self, step: str, key: str) -> str:
        """`steps.<step>.outputs.<key>`, empty when it was never written."""
        value: str = self.data["outputs"].get(step, {}).get(key, "")
        return value


def job_directory(env: Mapping[str, str]) -> Path | None:
    """This job's directory under RUNNER_TEMP, or None when the environment
    does not say which job this is. The name is the run id's alone, so
    `finish` finds it without anything `prepare` would have had to export."""
    temp, run_id = env.get("RUNNER_TEMP", ""), env.get("GITHUB_RUN_ID", "")
    if not temp.startswith("/") or not RUN_ID.match(run_id):
        return None
    return Path(temp) / f"deploy-daemon-{run_id}"


def present(directory: Path) -> bool:
    return directory.is_symlink() or directory.exists()


def trusted(directory: Path) -> bool:
    """Ours alone: a real directory this user owns and nobody else can write
    to. The state names a hold to release and the home to act on, so one that
    another account could have planted is not acted on."""
    try:
        info = directory.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022


def load_state(directory: Path) -> State | None:
    """The state `prepare` left, None when it never wrote any. Raises
    ValueError for a file that is not what `prepare` writes."""
    path = directory / "state.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema") != 1
        or not all(isinstance(data.get(key), dict) for key in ("settings", "steps", "outputs"))
    ):
        raise ValueError("not a deploy state file")
    settings = data["settings"]
    if (
        not HOLD_NAME.match(str(settings.get("hold", "")))
        or not str(settings.get("home", "")).startswith("/")
        or not VARIABLE.match(str(settings.get("token_env", "")))
    ):
        raise ValueError("deploy state names no usable hold or home")
    return State(directory, data)


# -- what the workflow's first step and job-level env settle ---------------------


def resolve_home(env: Mapping[str, str]) -> str:
    """ "Resolve host paths": the repository variable, else the runner's own
    LANTERN_HOME, else the default; fail closed on a root no step could
    address.

    The workflow's extra stop for a host that predates the rename is not
    repeated: this repository keeps the old name to an allow-list
    (tests/unit/test_brand.py). Such a host has no home here, so the first
    helper call fails instead, with nothing on the host touched."""
    home = env.get("HOME")
    root = env.get("LANTERN_HOME_VAR", "") or env.get("LANTERN_HOME", "")
    if not root:
        if home is None:
            raise StepFailed("HOME is not set, and neither is the lantern home")
        root = f"{home}/.lantern"
    if root == "~" or root.startswith("~/"):
        if home is None:
            raise StepFailed("HOME is not set; a '~' lantern home cannot be expanded")
        root = home if root == "~" else f"{home}/{root[2:]}"
    if "\n" in root:
        raise StepFailed("the lantern home may not contain a newline")
    if not root.startswith("/"):
        raise StepFailed(f"the lantern home must be an absolute path, not '{root}'")
    return root


def host_paths(root: str) -> dict[str, str]:
    """What that step exported: every path the job addresses, from one root."""
    return {
        "LANTERN_HOME": root,
        "LANTERN": f"{root}/bin/lantern",
        "VENV_LANTERN": f"{root}/venv/bin/lantern",
        "VENV_PYTHON": f"{root}/venv/bin/python",
        "UV": f"{root}/bin/uv",
        "UV_CACHE_DIR": f"{root}/cache/uv",
        "UV_PYTHON_INSTALL_DIR": f"{root}/python",
    }


def required(env: Mapping[str, str], name: str, what: str) -> str:
    value = env.get(name, "")
    if not value:
        raise StepFailed(f"{name} is not set ({what})")
    return value


def token_variable(args: argparse.Namespace, env: Mapping[str, str], recorded: str = "") -> str:
    name: str = args.token_env or env.get("DEPLOY_TOKEN_ENV", "") or recorded or DEFAULT_TOKEN_ENV
    if not VARIABLE.match(name):
        raise StepFailed(f"'{name}' is not the name of an environment variable")
    return name


def job_settings(args: argparse.Namespace, env: Mapping[str, str]) -> dict[str, Any]:
    """Everything the workflow took from its job-level `env:` and the github
    context, settled once so the three entry points cannot disagree."""
    run_id = env.get("GITHUB_RUN_ID", "")
    if not RUN_ID.match(run_id):
        raise StepFailed(f"GITHUB_RUN_ID must be one plain token, not '{run_id}'")
    if job_directory(env) is None:
        raise StepFailed("RUNNER_TEMP is not set to an absolute directory")
    home = resolve_home(env)
    repository = required(env, "GITHUB_REPOSITORY", "the repository whose releases deploy")
    host = required(env, "HOST", "the host, as the notices name it")
    channel = required(env, "DEPLOY_CHANNEL", "where the deploy notices go")
    run_url = env.get("RUN_URL", "")
    if not run_url:
        server = required(
            env, "GITHUB_SERVER_URL", "or set RUN_URL: the link the notices give for this run"
        )
        run_url = f"{server}/{repository}/actions/runs/{run_id}"
    prefix = args.hold_prefix
    if prefix is None:
        prefix = env.get("DEPLOY_HOLD_PREFIX", "")
    hold = f"{prefix}deploy-{run_id}"
    if not HOLD_NAME.match(hold):
        raise StepFailed(
            f"invalid hold name '{hold}': letters, digits, '.', '_' and '-' only (max 64)"
        )
    return {
        "home": home,
        "repository": repository,
        "run_id": run_id,
        "run_url": run_url,
        "host": host,
        "channel": channel,
        "unit": env.get("UNIT", "") or DEFAULT_UNIT,
        "hold": hold,
        # Manual means a dispatch, and nothing else: the workflow's MANUAL.
        "manual": env.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch",
        "input_version": env.get("INPUT_VERSION", ""),
        "token_env": token_variable(args, env),
    }


# -- reading the daemon -----------------------------------------------------------


def _field(value: Any, key: str) -> Any:
    """jq's `.key`: null stays null, and anything but an object is an error."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"cannot read .{key} of {type(value).__name__}")
    return value.get(key)


def _text(value: Any) -> str:
    """jq's string interpolation: a string as itself, anything else as JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def describe(status: str) -> str:
    """The workflow's CURRENT_JQ over one `ctl status --json` answer: the run
    in flight, else the claim in progress (a restart there orphans the
    issue), else "idle". Raises ValueError where jq would have failed."""
    answer = json.loads(status)
    current = _field(answer, "current")
    if current is not None:
        return f"{_text(_field(current, 'run_id'))} {DASH} {_text(_field(current, 'title'))}"
    claiming = _field(answer, "claiming")
    if claiming is not None and claiming is not False:
        return f"claiming {_text(claiming)}"
    return "idle"


def second_field(text: str) -> str:
    """`awk '{print $2}'` over a command's output, as `$( )` captures it."""
    fields = [line.split() for line in text.splitlines()]
    return "\n".join(line[1] if len(line) > 1 else "" for line in fields).rstrip("\n")


def helper_module() -> Any:
    """The copy of release_pipeline.py beside this file, for what it knows
    about the deployment state (never a second copy of that knowledge)."""
    spec = importlib.util.spec_from_file_location("release_pipeline", PIPELINE)
    if spec is None or spec.loader is None:
        raise StepFailed(f"cannot load {PIPELINE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the job -----------------------------------------------------------------------


class Job:
    """One deploy job: its settings, its state, and how it runs commands."""

    def __init__(
        self, env: Mapping[str, str], state: State, token_env: str, sleep: Sleep, clock: Clock
    ) -> None:
        self.state = state
        self.sleep = sleep
        self.clock = clock
        settings = state.settings
        self.home: str = settings["home"]
        self.paths = host_paths(self.home)
        self.lantern = self.paths["LANTERN"]
        self.venv_lantern = self.paths["VENV_LANTERN"]
        self.host: str = settings["host"]
        self.unit: str = settings["unit"]
        self.hold: str = settings["hold"]
        self.manual: bool = settings["manual"]
        self.run_url: str = settings["run_url"]
        self.run_id: str = settings["run_id"]
        # The token is for GitHub, under GH_TOKEN, for the helper alone.
        self.token = env.get(token_env, "") or env.get("GH_TOKEN", "")
        withheld = {"GH_TOKEN", "GITHUB_TOKEN", DEFAULT_TOKEN_ENV, token_env, *DOCKERHUB}
        self.dockerhub = {name: env.get(name, "") for name in DOCKERHUB}
        # What every command of the job sees: the step's environment without
        # the token or the analyzer's credentials, plus what the workflow's
        # first step exported and its job-level `env:` set.
        self.environment = {key: value for key, value in env.items() if key not in withheld}
        self.environment.update(self.paths)
        self.environment.update(
            MANUAL="true" if self.manual else "false",
            INPUT_VERSION=settings["input_version"],
            GITHUB_REPOSITORY=settings["repository"],
            HOST=self.host,
            DEPLOY_CHANNEL=settings["channel"],
            UNIT=self.unit,
            HOLD=self.hold,
        )

    # -- running things ------------------------------------------------------------

    def call(
        self,
        *argv: str,
        capture: bool = False,
        feed: str | None = None,
        no_stdin: bool = False,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run one command and return its exit code and, when asked, its
        stdout. A command that cannot be started, or that outlives
        *timeout*, is a failed command, as it was in the shell."""
        sys.stdout.flush()
        options: dict[str, Any] = {}
        if feed is not None:
            options["input"] = feed
        elif no_stdin:
            options["stdin"] = subprocess.DEVNULL
        try:
            done = subprocess.run(
                list(argv),
                env=dict(self.environment if env is None else env),
                stdout=subprocess.PIPE if capture else None,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                **options,
            )
        except subprocess.TimeoutExpired:
            say(f"::error::{argv[0]} did not finish within {timeout:.0f}s")
            return 124, ""
        except OSError as error:
            say(f"::error::cannot run {argv[0]}: {error}")
            return 127, ""
        return done.returncode, done.stdout or ""

    def must(self, *argv: str, **options: Any) -> str:
        code, output = self.call(*argv, **options)
        if code != 0:
            raise StepFailed("")
        return output

    def pipeline(
        self, mode: str, *, token: bool = False, **extra: str
    ) -> tuple[int, dict[str, str]]:
        """One release_pipeline.py subcommand under the home's interpreter,
        as the workflow runs it. Returns its exit code and the step outputs
        it wrote. Only the modes the workflow gave its token get one."""
        outputs = self.state.directory / "outputs"
        outputs.write_text("", encoding="utf-8")
        env = {**self.environment, **extra, "GITHUB_OUTPUT": str(outputs)}
        if token and self.token:
            env["GH_TOKEN"] = self.token
        code, _ = self.call(self.paths["VENV_PYTHON"], str(PIPELINE), mode, env=env)
        written: dict[str, str] = {}
        for line in outputs.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                written[key] = value
        return code, written

    def notify(self, text: str) -> None:
        """A notice through the installed notifier. continue-on-error: a
        chat outage must not fail, or roll back, a deploy."""
        code, _ = self.pipeline("deploy-notify", DEPLOY_NOTICE=text)
        if code != 0:
            say("::warning::could not post the deploy notice")

    @contextlib.contextmanager
    def step(self, name: str) -> Iterator[None]:
        """One step of the workflow: its outcome is on record before its
        first command runs and after its last, whatever kills the job."""
        self.state.begin(name)
        try:
            yield
        except StepFailed as failure:
            if str(failure):
                say(f"::error::{failure}")
            self.state.end(name, "failure")
            raise
        self.state.end(name, "success")

    # -- prepare ---------------------------------------------------------------------

    def decide(self, step: str) -> dict[str, str]:
        """ "Compare against what is installed" and, after the wait, "Refresh
        the target after draining": the helper's deploy-check."""
        with self.step(step):
            code, outputs = self.pipeline("deploy-check", token=True)
            self.state.data["outputs"][step] = outputs
            self.state.save()
            if code != 0:
                raise StepFailed("")
        return outputs

    def wait_out_cooldown(self) -> None:
        """Sleep until the cooldown the helper reported has passed. There is
        no schedule to pick the release up later, so the job waits here,
        before doctor and before any hold, and then decides again."""
        helper = helper_module()
        try:
            last = helper.read_state(Path(self.home) / "state" / "deploy.json")
        except (ValueError, OSError) as error:
            raise StepFailed(f"cannot read the deployment state: {error}") from error
        ends = int(last.get("finished_at", 0)) + int(helper.DEPLOY_COOLDOWN_SECONDS)
        # Never less than one poll: two clocks that disagree must not spin.
        remaining = max(ends - int(self.clock()), 15)
        say(f"cooldown: deciding again in {remaining}s; nothing on the host is touched until then")
        self.sleep(remaining)

    def check_host(self) -> None:
        """ "Check the host before upgrading": an existing login or host
        failure cannot be repaired by rolling the package back."""
        with self.step("doctor"):
            self.must(self.lantern, "doctor")

    def prepare_analyzer(self) -> None:
        """ "Prepare isolated PDF analyzer": a separate sbx app with no agent
        credentials or shared policy, before the hold or the install."""
        with self.step("analyzer"):
            username, secret = (self.dockerhub[name] for name in DOCKERHUB)
            for name in DOCKERHUB:
                if not self.dockerhub[name]:
                    raise StepFailed(f"{name} is not set")
            sbx = f"{self.home}/bin/sbx"
            env = {**self.environment, **self.dockerhub}
            self.must(
                sbx,
                "--app-name",
                ANALYSIS_APP,
                "login",
                "--username",
                username,
                "--password-stdin",
                feed=secret,
                env=env,
            )
            marker = Path(self.home) / "state" / "channel-pdf-policy-v2"
            if not marker.exists():
                self.must(sbx, "--app-name", ANALYSIS_APP, "policy", "init", "deny-all", env=env)
                self.must(
                    sbx, "--app-name", ANALYSIS_APP, "policy", "deny", "network", "**", env=env
                )
                try:
                    os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
                except OSError as error:
                    raise StepFailed(f"cannot write {marker}: {error}") from error
            self.must(sbx, "--app-name", ANALYSIS_APP, "version", env=env)

    def take_hold(self) -> str:
        """ "Take the deploy hold". Exit 2 = no daemon answered: not fatal
        here, the wait notices a daemon that never answers. The step is on
        record before the pause is sent, so `finish` releases a hold the
        job asked for even when the job died before hearing the answer."""
        with self.step("hold"):
            self.call(
                self.lantern, "daemon", "ctl", "pause", "--hold", self.hold, "--timeout", "30"
            )
            _, status = self.call(
                self.lantern, "daemon", "ctl", "status", "--json", "--timeout", "30", capture=True
            )
            say(status.rstrip("\n"))
            try:
                current = describe(status) if status.strip() else ""
            except ValueError:
                current = ""
            current = current or "unknown"
            self.state.record("hold", current=current)
        return current

    def announce(self, current: str) -> None:
        """ "Announce" (continue-on-error)."""
        installed, target = (self.state.output("installed", key) for key in ("current", "version"))
        if current in ("idle", "unknown"):
            text = (
                f"{ROCKET} deploying lantern `{installed}` {ARROW} `{target}` on `{self.host}`"
                f" {DASH} [run]({self.run_url})"
            )
        else:
            text = (
                f"{ROCKET} lantern `{installed}` {ARROW} `{target}` staged on `{self.host}`"
                f" {DASH} restart deferred until the daemon is idle (now: {current});"
                f" hold `{self.hold}` {DASH} [run]({self.run_url})"
            )
        self.notify(text)

    def wait_for_idle(self) -> None:
        """ "Wait for the daemon to go idle". No cap: a restart under a live
        run kills its in-flight task. With the hold taken nothing new is
        claimed, so idle stays idle. Poll no faster than 15 s (status()
        touches the circuit breaker). A daemon that answers nothing for
        five minutes straight has nothing to drain; exit 1 means it answered
        without a structured status, and guessing there is the restart
        under a run this exists to prevent."""
        with self.step("drain"):
            waited = misses = 0
            while True:
                code, status = self.call(
                    self.lantern,
                    "daemon",
                    "ctl",
                    "status",
                    "--json",
                    "--timeout",
                    "30",
                    capture=True,
                )
                if code == 0:
                    misses = 0
                    try:
                        # jq prints nothing for no input: not "idle".
                        current = describe(status) if status.strip() else ""
                    except ValueError as error:
                        raise StepFailed(f"cannot read the daemon's status: {error}") from error
                    if current == "idle":
                        say(f"daemon is idle after {waited}s {DASH} safe to restart")
                        break
                    say(f"not idle ({current}) {DASH} waiting ({waited}s so far)")
                elif code == 1:
                    raise StepFailed(
                        "the daemon answered ctl status but gave no structured status"
                        f" {DASH} upgrade it by hand once (contrib/systemd/README.md),"
                        " then redeploy"
                    )
                else:
                    misses += 1
                    say(f"no answer from the daemon ({misses}/20)")
                    if misses >= 20:
                        say(
                            "::warning::no daemon answered ctl status for 5 min"
                            f" {DASH} nothing to drain; restarting"
                        )
                        break
                self.sleep(15)
                waited += 15
            self.state.record("drain", waited_s=str(waited))

    # -- deploy ----------------------------------------------------------------------

    def fetch_wheels(self, version: str) -> str:
        """ "Fetch the release wheels": the GitHub Release is the only
        distribution. The helper downloads the exact wheel names, checks
        size and digest, then checks them against the release manifest."""
        with self.step("wheels"):
            dist = self.state.directory / "wheels"
            dist.mkdir(exist_ok=True)
            self.state.record("wheels", dist=str(dist))
            for attempt in range(1, 6):
                code, _ = self.pipeline(
                    "download-wheels", token=True, VERSION=version, DIST=str(dist)
                )
                if code == 0:
                    for name in sorted(item.name for item in dist.iterdir()):
                        say(name)
                    code, _ = self.pipeline(
                        "verify-download", token=True, VERSION=version, DIST=str(dist)
                    )
                    if code != 0:
                        raise StepFailed("")
                    return str(dist)
                say(f"release v{version} not downloadable yet (attempt {attempt}); retrying in 15s")
                self.sleep(15)
            raise StepFailed(f"could not download the wheels for v{version}")

    def record_attempt(self, version: str) -> None:
        """ "Record the deployment attempt"."""
        with self.step("attempt"):
            code, _ = self.pipeline("deploy-start", token=True, VERSION=version)
            if code != 0:
                raise StepFailed("")

    def install(self, version: str, dist: str) -> None:
        """Both wheels together, from the release, with both chat extras:
        never by name from a package index."""
        self.must(
            self.paths["UV"],
            "pip",
            "install",
            "--python",
            self.paths["VENV_PYTHON"],
            "--upgrade",
            f"{dist}/lantern_worker-{version}-py3-none-any.whl",
            f"{dist}/lantern_backend-{version}-py3-none-any.whl[discord,slack]",
        )

    def upgrade(self, version: str, dist: str) -> None:
        """ "Upgrade": a snapshot first, then the wheels, then init, which
        refreshes the launchers and units and keeps the operator's sbx."""
        previous = self.state.output("installed", "current")
        with self.step("upgrade"):
            self.must(
                self.lantern,
                "backup",
                "--label",
                f"deploy-{version.replace('.', '-')}",
                "--reason",
                f"deploy {previous} -> {version}",
            )
            self.install(version, dist)
            self.must(self.venv_lantern, "--version")
            self.must(self.lantern, "init", "--systemd", "--no-sbx")
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
            try:
                log = Path(self.home) / "logs" / "deploy"
                log.mkdir(parents=True, exist_ok=True)
                with (log / "history.log").open("a", encoding="utf-8") as history:
                    history.write(f"{stamp} {previous} -> {version} run={self.run_id}\n")
            except OSError as error:
                raise StepFailed(f"cannot write the deploy history: {error}") from error

    def restart(self) -> None:
        """ "Restart the daemon". reset-failed first: a unit that crash-looped
        sits `failed` and a plain restart will not revive it. The stop half
        alone can take three minutes, hence the 300 s."""
        with self.step("restart"):
            self.call("systemctl", "--user", "reset-failed", self.unit)
            self.must("systemctl", "--user", "restart", self.unit, timeout=300)

    def is_active(self) -> bool:
        return self.call("systemctl", "--user", "is-active", "--quiet", self.unit)[0] == 0

    def answers(self) -> bool:
        """The daemon answers `ctl status`. The budget covers its whole
        startup, not one round trip: the control server starts only after
        recovery, which has taken over a minute."""
        code, _ = self.call(
            self.lantern, "daemon", "ctl", "status", "--json", "--timeout", "300", capture=True
        )
        return code == 0

    def restart_count(self) -> str:
        return self.must(
            "systemctl", "--user", "show", self.unit, "-p", "NRestarts", "--value", capture=True
        ).rstrip("\n")

    def health_check(self, version: str) -> None:
        """ "Health check": unit active, the version is the one deployed,
        plain doctor (never --deep: that boots a microVM), the daemon
        answers, and 45 s later it has not died or restarted."""

        def fail(why: str) -> StepFailed:
            return StepFailed(f"health check failed: {why}")

        with self.step("health"):
            if not self.is_active():
                raise fail("unit is not active")
            got = second_field(self.must(self.venv_lantern, "--version", capture=True))
            if got != version:
                raise fail(f"version is {got}, expected {version}")
            if self.call(self.lantern, "doctor")[0] != 0:
                raise fail("lantern doctor is unhappy")
            if not self.answers():
                raise fail("daemon did not answer ctl status")
            before = self.restart_count()
            self.sleep(45)
            if not self.is_active():
                raise fail("unit died within 45s of starting")
            after = self.restart_count()
            if before != after:
                raise fail(f"unit restarted {before} -> {after} within 45s (crash loop)")
            say(f"::notice::{self.host} is healthy on {version}")

    def refresh_template(self) -> None:
        """ "Refresh the baked template" (continue-on-error), on the hosts
        that have baked before: the bake record is their opt-in."""
        record = Path(self.home) / "state" / "bake.json"
        if not record.is_file():
            say(f"no bake record at {record}; this host does not use a baked template")
            return
        if self.call(self.lantern, "bake", no_stdin=True)[0] != 0:
            say("::warning::could not refresh the baked template; the deploy is healthy without it")

    def roll_back(self) -> None:
        """ "Roll back", once the upgrade ran: the previous version from its
        own GitHub Release, through the same checked helper, with the same
        extras, and "restored" only after the same health checks pass."""
        previous = self.state.output("installed", "current")
        with self.step("rollback"):
            self.state.record("rollback", restored="false")
            say(f"::error::deploy failed {DASH} rolling back to {previous}")
            dist = self.state.directory / "previous-wheels"
            dist.mkdir(exist_ok=True)
            try:
                for attempt in (1, 2, 3):
                    code, _ = self.pipeline(
                        "download-wheels", token=True, VERSION=previous, DIST=str(dist)
                    )
                    if code == 0:
                        break
                    if attempt == 3:
                        raise StepFailed(f"could not download the wheels for v{previous}")
                    say(
                        f"release v{previous} not downloadable (attempt {attempt}); retrying in 15s"
                    )
                    self.sleep(15)
                code, _ = self.pipeline(
                    "verify-download", token=True, VERSION=previous, DIST=str(dist)
                )
                if code != 0:
                    raise StepFailed("")
                self.install(previous, str(dist))
                self.call(self.lantern, "init", "--systemd", "--no-sbx")
                self.call("systemctl", "--user", "reset-failed", self.unit)
                self.must("systemctl", "--user", "restart", self.unit, timeout=300)
                self.sleep(15)
                # A running process alone does not establish a rollback: the
                # same checks, each only if the one before it passed. Inside
                # the shell's `if`, a --version that fails was a mismatch.
                restored = (
                    self.is_active()
                    and second_field(self.call(self.venv_lantern, "--version", capture=True)[1])
                    == previous
                    and self.call(self.lantern, "doctor")[0] == 0
                    and self.answers()
                )
                if not restored:
                    raise StepFailed(f"ROLLBACK ALSO FAILED {DASH} {self.host} needs a human")
                self.state.record("rollback", restored="true")
                say(f"rolled back to {previous}; health checks passed")
            finally:
                shutil.rmtree(dist, ignore_errors=True)

    # -- finish ----------------------------------------------------------------------

    def record_outcome(self) -> bool:
        """ "Record the deployment outcome": only for an attempt that was
        recorded and an upgrade that started. The helper decides what the
        health and rollback results mean for the next automatic deploy."""
        code, _ = self.pipeline(
            "deploy-finish",
            HEALTH=self.state.outcome("health"),
            RESTORED=self.state.output("rollback", "restored"),
        )
        return code == 0

    def release_hold(self) -> None:
        """ "Release the deploy hold": holds survive the restart, so ours
        must not outlive the job. An operator's own hold is not touched."""
        code, _ = self.call(
            self.lantern, "daemon", "ctl", "resume", "--hold", self.hold, "--timeout", "60"
        )
        if code != 0:
            say(f"::warning::could not release hold {self.hold}")
        self.call(self.lantern, "daemon", "ctl", "status", "--timeout", "60")

    def report(self, result: str) -> None:
        """ "Report" (continue-on-error): the workflow's five messages."""
        state = self.state
        version = state.output("final", "version") or state.output("installed", "version")
        installed = state.output("installed", "current")
        upgraded = state.outcome("upgrade")
        link = f"[run]({self.run_url})"
        failed = f"{CROSS} deploy of lantern `{version}` to `{self.host}` **{result}**"
        waited = ""
        seconds = state.output("drain", "waited_s")
        if seconds.isdigit() and int(seconds) > 0:
            waited = f" after waiting {int(seconds) // 60} min for the daemon to go idle"
        if result == "success" and upgraded == "skipped":
            text = (
                f"Deploy batch on {self.host} finished without an upgrade"
                f" ({state.output('final', 'reason')}); daemon remains on {installed}. {link}"
            )
        elif result == "success":
            text = f"{CHECK} `{self.host}` restarted on lantern `{version}`{waited} {DASH} {link}"
        elif upgraded == "skipped":
            text = (
                f"{failed} before anything was installed {DASH} still on `{installed}`,"
                f" daemon untouched. {link}"
            )
        elif state.output("rollback", "restored") == "true":
            text = f"{failed} {DASH} rolled back to `{installed}`. {link}"
        else:
            text = (
                f"{failed} {DASH} rollback could not restore health;"
                f" {self.host} needs a human. {link}"
            )
        self.notify(text)


# -- the three entry points -----------------------------------------------------------


def prepare(args: argparse.Namespace, env: Mapping[str, str], sleep: Sleep, clock: Clock) -> int:
    try:
        settings = job_settings(args, env)
        directory = job_directory(env)
        assert directory is not None  # job_settings checked it
        if present(directory) and not trusted(directory):
            raise StepFailed(f"{directory} is not this job's own directory; refusing to use it")
        try:
            if present(directory):
                # The same run id again (a re-run): start over.
                shutil.rmtree(directory)
            directory.mkdir(mode=0o700)
        except OSError as error:
            raise StepFailed(f"cannot create {directory}: {error}") from error
    except StepFailed as failure:
        say(f"::error::{failure}")
        return 1
    state = State(directory, {"schema": 1, "settings": settings, "steps": {}, "outputs": {}})
    state.save()
    job = Job(env, state, settings["token_env"], sleep, clock)
    try:
        found = job.decide("installed")
        while args.wait_cooldown and not job.manual and found.get("reason") == "cooldown":
            job.wait_out_cooldown()
            found = job.decide("installed")
        if found.get("changed") == "true":
            job.check_host()
            job.prepare_analyzer()
            job.announce(job.take_hold())
            job.wait_for_idle()
        else:
            say(f"nothing to deploy ({found.get('reason', 'no decision')}): the host is untouched")
    except StepFailed:
        return 1
    state.data["prepared"] = True
    state.save()
    return 0


def open_job(
    args: argparse.Namespace, env: Mapping[str, str], sleep: Sleep, clock: Clock
) -> Job | None:
    """The job `prepare` started, from the state it left; None when it left
    none. Raises ValueError for a state that cannot be trusted or read."""
    directory = job_directory(env)
    if directory is None or not present(directory):
        return None
    if not trusted(directory):
        raise ValueError(f"{directory} is not this job's own directory")
    try:
        state = load_state(directory)
        if state is None:
            return None
        return Job(env, state, token_variable(args, env, state.settings["token_env"]), sleep, clock)
    except (StepFailed, ValueError, KeyError, TypeError, OSError) as error:
        raise ValueError(f"cannot use the deploy state in {directory}: {error}") from error


def deploy(args: argparse.Namespace, env: Mapping[str, str], sleep: Sleep, clock: Clock) -> int:
    try:
        job = open_job(args, env, sleep, clock)
    except ValueError as failure:
        say(f"::error::{failure}")
        return 1
    if job is None or job.state.data.get("prepared") is not True or "deploy" in job.state.data:
        # After a failed step GitHub skips the rest, and so does this: the
        # wait for idle in particular must have finished.
        say("::error::`prepare` did not complete for this job, or `deploy` already ran")
        return 1
    state = job.state
    if state.output("installed", "changed") != "true":
        say("nothing to deploy: `prepare` found the host needs no upgrade")
        return 0
    state.data["deploy"] = "running"
    state.save()
    try:
        final = job.decide("final")
        if final.get("changed") == "true":
            version = final.get("version", "")
            if version != state.output("installed", "version"):
                # "Announce the selected release" (continue-on-error).
                job.notify(
                    f"Deploy batch refreshed after draining: installing {version} on {job.host}."
                )
            dist = job.fetch_wheels(version)
            job.record_attempt(version)
            job.upgrade(version, dist)
            job.restart()
            job.health_check(version)
            job.refresh_template()
        else:
            say(f"no upgrade after draining ({final.get('reason', 'no decision')})")
    except StepFailed:
        # Only once the upgrade ran: a failure before it changed nothing on
        # the host, and a rollback restart would be the needless restart
        # this pipeline exists to avoid.
        if state.output("final", "changed") == "true" and state.outcome("upgrade") != "skipped":
            with contextlib.suppress(StepFailed):
                job.roll_back()
        state.data["deploy"] = "failure"
        state.save()
        return 1
    state.data["deploy"] = "success"
    state.save()
    return 0


def finish(args: argparse.Namespace, env: Mapping[str, str], sleep: Sleep, clock: Clock) -> int:
    directory = job_directory(env)
    try:
        job = open_job(args, env, sleep, clock)
    except ValueError as failure:
        say(f"::error::{failure}")
        say(
            "::error::if this job paused the daemon, its hold still stands: find it with"
            " `lantern daemon ctl status` and release it with"
            " `lantern daemon ctl resume --hold <name>`"
        )
        return 1
    if job is None:
        say("no deploy state for this job: nothing to finish")
        if directory is not None and present(directory):
            # `prepare` died between making its directory and its first record.
            shutil.rmtree(directory, ignore_errors=True)
        return 0
    state = job.state

    given = args.status if args.status is not None else env.get("DEPLOY_JOB_STATUS", "")
    result = JOB_STATUS.get(given)
    if result is None:
        say(f"::warning::job status '{given}' is not one this knows; reporting a failure")
        result = "failure"

    # Each of these ran under always(): one that fails does not stop the next.
    failed = False
    if state.outcome("attempt") == "success" and state.outcome("upgrade") != "skipped":
        failed = not _always(job.record_outcome) or failed
    if state.outcome("hold") != "skipped":
        failed = not _always(job.release_hold) or failed
    # "Clean up the downloaded wheels": they are in the job's own directory.
    for name in ("wheels", "previous-wheels"):
        shutil.rmtree(state.directory / name, ignore_errors=True)
    if state.output("installed", "changed") == "true":
        # job.status, as a step that follows a failed one sees it.
        _always(job.report, "failure" if failed and result == "success" else result)
    # "Clean up the workflow helper": everything else the job kept.
    shutil.rmtree(state.directory, ignore_errors=True)
    return 1 if failed else 0


def _always(step: Callable[..., Any], *args: Any) -> bool:
    """Run one of `finish`'s steps. Whatever it raises fails that step and
    no other; False when it raised or said it failed."""
    try:
        return step(*args) is not False
    except Exception as error:
        say(f"::error::{getattr(step, '__name__', 'step')}: {error}")
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy_daemon.py",
        description="lantern's own deploy as three steps of one job: prepare, deploy, finish.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    token = argparse.ArgumentParser(add_help=False)
    token.add_argument(
        "--token-env",
        metavar="NAME",
        default=None,
        help="the environment variable holding the job's GitHub token"
        f" (default: $DEPLOY_TOKEN_ENV, else {DEFAULT_TOKEN_ENV}; GH_TOKEN is the fallback)",
    )
    first = commands.add_parser(
        "prepare", parents=[token], help="decide, check the host, take the hold, wait for idle"
    )
    first.add_argument(
        "--wait-cooldown",
        action="store_true",
        help="outlast a deploy cooldown and decide again, instead of ending the job",
    )
    first.add_argument(
        "--hold-prefix",
        metavar="PREFIX",
        default=None,
        help="put PREFIX before `deploy-<run id>` in the hold's name"
        " (default: $DEPLOY_HOLD_PREFIX)",
    )
    commands.add_parser(
        "deploy", parents=[token], help="refresh the target, upgrade, restart, check, roll back"
    )
    last = commands.add_parser(
        "finish", parents=[token], help="record the outcome, release the hold, clean up, report"
    )
    last.add_argument(
        "--status",
        default=None,
        help="how the job went: success, failure, timed_out or cancelled"
        " (default: $DEPLOY_JOB_STATUS)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    sleep: Sleep = time.sleep,
    clock: Clock = time.time,
) -> int:
    args = _parser().parse_args(argv)
    entry = {"prepare": prepare, "deploy": deploy, "finish": finish}[args.command]
    return entry(args, dict(os.environ if env is None else env), sleep, clock)


if __name__ == "__main__":
    sys.exit(main())
