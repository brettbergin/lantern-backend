"""Installed versus latest: is this daemon running current code?

Lantern's own releases ship frequently (``RELEASING.md``) while upgrading a
daemon host is an operator's step, so a long-lived daemon drifts behind
silently. This module is the one place that knows the difference: the
concierge's ``version_status`` tool renders :meth:`VersionProbe.summary` on
demand, and the daemon posts :meth:`VersionProbe.drift_notice` to the control
channel once at startup when it is behind — a tool only helps the people who
think to ask.

"Latest" is the newest published GitHub Release (drafts and prereleases never
count), read from GitHub's REST API. This is the **only outbound HTTP the host
itself makes** apart from the optional chat bridge and ``daemon notify``;
everything else, all other GitHub access included, is deliberately proxied
through a sandbox (see :mod:`lantern.vcs.github.ops`). The request is
unauthenticated and carries no credential, so the credential split is
untouched. It is bounded by a short timeout and a response cap, memoised for
:data:`LATEST_TTL_S`, and every failure degrades to "could not reach GitHub
Releases" rather than raising: a version report is a nicety, never a reason to
break a turn or delay a daemon start. ``[daemon] version_check = false``
switches the release half off entirely (#641) — an air-gapped or mirror-pinned
host, or one a deploy pipeline keeps current, makes no request and hears no
advice; the installed half still answers.

Upgrading is deliberately not here. A daemon that upgrades and restarts
itself mid-run is a different, riskier feature. What the operator runs to
upgrade depends on how lantern was installed — ``lantern update`` in an
initialised home, a container image, a deploy pipeline — so the advice names
``[daemon] upgrade_command`` when the operator set one and otherwise says
exactly that (#638).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, NamedTuple

import lantern
import lantern_worker
from lantern import releases
from lantern.errors import SbxError, SbxNotFoundError
from lantern.log import get_logger

if TYPE_CHECKING:
    from lantern.sbx.cli import SbxCLI

log = get_logger(__name__)


LATEST_RELEASE_URL = releases.LATEST_RELEASE_URL
LATEST_TIMEOUT_S = 4.0
# The answer changes at most once per merge to main, and one turn may call the
# tool several times: memoise successes for a few minutes. Follows the house
# rate-limit shape (a timestamp plus a ``now - last < TTL`` guard, as in
# DaemonGithub.note_failure) rather than introducing a cache abstraction.
LATEST_TTL_S = 300.0
# One release object (its notes and asset list included) is a few KB. Cap the
# read so a pathological response is a miss rather than a memory problem.
MAX_BYTES = 2_000_000
# The never-built fallback in both packages' __init__: not a real version, so
# it must not be compared against anything.
UNBUILT = "0.0.0"
# What the daemon host installs. Both are reported, though one GitHub Release
# carries both wheels in lockstep, so they share one "latest".
DISTRIBUTIONS = ("lantern-backend", "lantern-worker")

Verdict = Literal["behind", "current", "ahead", "dev", "unknown"]


class InstalledVersions(NamedTuple):
    lantern: str
    worker: str
    sbx: str | None


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: max(0, limit - 1)].rstrip() + "…"


def _release(version: str) -> tuple[int, ...] | None:
    """The leading run of integer dot-chunks: ``0.7.12.dev0`` → ``(0, 7, 12)``.

    hatch-vcs only ever produces ``X.Y.Z`` or ``X.Y.Z.devN``
    (``tests/unit/test_version.py`` pins that), so this is the whole grammar
    worth parsing — and parsing it here is why ``packaging`` stays out of the
    dependency list for one comparison.
    """
    parts: list[int] = []
    for chunk in version.split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts) or None


def _is_dev(version: str) -> bool:
    return ".dev" in version


def _has_suffix(version: str) -> bool:
    """Is there anything after the leading run of integer dot-chunks?

    ``0.7.15`` no; ``0.8.0rc1``, ``0.7.15.post1``, ``0.7.12.dev0`` yes.
    """
    chunks = version.split(".")
    release = 0
    for chunk in chunks:
        if not chunk.isdigit():
            break
        release += 1
    return release != len(chunks)


def compare(installed: str, latest: str | None) -> Verdict:
    """How ``installed`` stands against the newest published release.

    Two cases refuse to answer rather than answer wrongly:

    ``0.0.0`` is the never-built fallback, not a version anyone shipped.

    A ``.devN`` build is named after the version it is heading *toward*, so
    ``0.7.12.dev0`` is past the ``0.7.11`` tag but is **not** ``0.7.12``.
    Truncating it to ``(0, 7, 12)`` would claim parity with a release it does
    not contain, so a development build never earns an upgrade verdict.
    """
    if latest is None or not installed or installed == UNBUILT:
        return "unknown"
    if _is_dev(installed):
        return "dev"
    if _has_suffix(latest):
        # A pre/post/dev version is not a stable release an update would
        # install, so ranking against it would produce advice that does not
        # work. Say nothing rather than something wrong.
        return "unknown"
    mine, theirs = _release(installed), _release(latest)
    if mine is None or theirs is None:
        return "unknown"
    if mine == theirs:
        return "current"
    return "behind" if mine < theirs else "ahead"


def behind_by(installed: str, latest: str | None) -> int | None:
    """How many patch releases separate the two, when only the patch differs.

    ``None`` whenever the gap is not a plain patch run — saying "3 patches
    behind" across a minor bump would be a lie.
    """
    if latest is None:
        return None
    mine, theirs = _release(installed), _release(latest)
    if mine is None or theirs is None or len(mine) < 3 or len(theirs) < 3:
        return None
    if mine[:2] != theirs[:2] or theirs[2] <= mine[2]:
        return None
    return theirs[2] - mine[2]


def fetch_latest(name: str, *, timeout_s: float = LATEST_TIMEOUT_S) -> str | None:
    """The newest published release's version, or ``None``.

    Both distributions ship in one GitHub Release, so ``name`` only labels
    the log line. The answer is the release's ``tag_name`` without its
    leading ``v``; a draft, a prerelease or any other tag shape is no answer.

    Never raises: every failure is an anticipated one (no egress, DNS, a
    GitHub outage or rate limit), so it is logged with ``error=`` and no
    traceback, per the house rule, and the caller reports "could not reach
    GitHub Releases".
    """
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"lantern/{lantern.__version__}",
        },
    )
    try:
        # nosec B310 - LATEST_RELEASE_URL is a constant https:// literal, not caller input
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # nosec B310
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        log.warning("versions.latest_failed", name=name, error=f"HTTP {exc.code}")
        return None
    except urllib.error.URLError as exc:
        log.warning("versions.latest_failed", name=name, error=str(exc.reason))
        return None
    except OSError as exc:  # socket timeouts and the rest
        log.warning("versions.latest_failed", name=name, error=str(exc))
        return None
    if len(raw) > MAX_BYTES:
        log.warning("versions.latest_failed", name=name, error=f"response over {MAX_BYTES} bytes")
        return None
    try:
        data = json.loads(raw)
        tag = data["tag_name"]
        published = data.get("draft") is False and data.get("prerelease") is False
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("versions.latest_failed", name=name, error=f"unparseable: {exc}")
        return None
    if not published or not isinstance(tag, str) or not tag.startswith("v") or len(tag) < 2:
        log.warning("versions.latest_failed", name=name, error=f"not a published release: {tag!r}")
        return None
    return tag[1:]


class VersionProbe:
    """Installed versions, plus — best effort — the latest release.

    ``fetch`` is injected so tests never touch the network, and ``clock``
    drives the TTL memo; both follow the constructor-injection pattern the
    daemon already uses for ``clock`` and ``store_factory``.

    Only *successful* lookups are memoised. Caching a failure would leave the
    tool useless for five minutes after one blip, and the cost of retrying is
    bounded by ``LATEST_TIMEOUT_S`` and the turn's own tool-call cap.
    """

    def __init__(
        self,
        *,
        sbx: SbxCLI | None = None,
        clock: Callable[[], float] = time.monotonic,
        fetch: Callable[[str], str | None] = fetch_latest,
        sbx_timeout_s: float = 5.0,
        check_releases: bool = True,
        upgrade_command: str | None = None,
    ) -> None:
        self.sbx = sbx
        self.clock = clock
        self.fetch = fetch
        self.sbx_timeout_s = sbx_timeout_s
        # `[daemon] version_check`: False never calls `fetch`, so the host
        # makes no request and every "latest" reads as unknown-by-choice.
        self.check_releases = check_releases
        # `[daemon] upgrade_command`: what the advice tells the operator to run.
        self.upgrade_command = upgrade_command
        self._latest: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    # -- reads ---------------------------------------------------------------

    def installed(self) -> InstalledVersions:
        """What this host is actually running. Purely local; never fails."""
        return InstalledVersions(
            lantern=lantern.__version__,
            worker=lantern_worker.__version__,
            sbx=self._sbx()[0],
        )

    def _sbx(self) -> tuple[str | None, str]:
        """The sbx CLI's version, or the reason there isn't one.

        The three failures read differently to an operator — no handle, no
        binary, a wedged Docker — so they are not collapsed into one line.
        None of them may sink the report: the release rows are the point.
        """
        if self.sbx is None:
            return None, "not configured for this daemon"
        try:
            version = self.sbx.version(timeout=self.sbx_timeout_s)
        except SbxNotFoundError:
            return None, "not found on PATH — this daemon cannot start sandboxes"
        except SbxError as exc:
            log.warning("versions.sbx_unavailable", error=_one_line(str(exc), 200))
            return None, f"could not be asked ({_one_line(str(exc), 100)})"
        if version is None:
            return None, "reported no recognisable version"
        return version, ""

    def latest(self, name: str) -> str | None:
        """The newest release of ``name``, memoised; ``None`` when GitHub
        Releases could not be reached — or was never asked (``check_releases``
        off)."""
        if not self.check_releases:
            return None
        now = self.clock()
        with self._lock:
            cached = self._latest.get(name)
            if cached is not None and now - cached[0] < LATEST_TTL_S:
                return cached[1]
        version = self.fetch(name)
        if version is not None:
            with self._lock:
                self._latest[name] = (now, version)
        return version

    # -- rendering -----------------------------------------------------------

    def summary(self) -> str:
        """The full report the concierge hands back.

        Read by the model *and*, via the model, by a human — so it says
        plainly that upgrading is a step someone takes on the host. The
        concierge cannot do it and must not imply otherwise.
        """
        installed = self.installed()
        lines: list[str] = []
        stale = False
        unreachable = False
        for name, mine in (("lantern", installed.lantern), ("lantern-worker", installed.worker)):
            newest = self.latest(name)
            if newest is None:
                unreachable = True
                why = (
                    "latest release not checked"
                    if not self.check_releases
                    else "could not reach GitHub Releases"
                )
                lines.append(f"{name:<15} {mine} installed · {why}")
                continue
            verdict = compare(mine, newest)
            note = {
                "behind": "BEHIND",
                "current": "up to date",
                "ahead": "ahead of the latest release",
                "dev": "a development build, not a release — comparison is approximate",
                "unknown": "cannot compare these",
            }[verdict]
            if verdict == "behind":
                stale = True
                gap = behind_by(mine, newest)
                if gap:
                    note += f" by {gap} patch release{'s' if gap != 1 else ''}"
            lines.append(f"{name:<15} {mine} installed · {newest} released · {note}")
        version, why = self._sbx()
        lines.append(f"{'sbx CLI':<15} {version or why}")
        if installed.lantern == UNBUILT:
            lines.append(
                "The installed version reads 0.0.0, which means this tree was never built — "
                "no upgrade advice follows from it."
            )
        if unreachable and not self.check_releases:
            lines.append(
                "The release check is off on this host ([daemon] version_check = false), so "
                "'latest' was not looked up; the installed versions are still accurate. Whether "
                "an upgrade is due is the operator's call, not something to infer from here."
            )
        elif unreachable:
            lines.append(
                "Could not reach GitHub Releases, so 'latest' is unknown for the rows above "
                "that say so; the installed versions are still accurate."
            )
        if stale:
            lines.append(
                "This daemon keeps running the code it started with. Upgrading is an "
                f"operator's step on the daemon host — {self.upgrade_hint()}, then restart "
                "the daemon. You cannot do it from here: say so."
            )
        return "\n".join(lines)

    def upgrade_hint(self) -> str:
        """What the operator runs to upgrade, as a clause. Names
        ``[daemon] upgrade_command`` when one is set; otherwise says the
        command depends on the install method rather than guessing one."""
        if self.upgrade_command:
            return f"run `{self.upgrade_command}`"
        return (
            "the exact command depends on how lantern was installed (`lantern update` in an "
            "initialised home, a container image, a deploy pipeline)"
        )

    def drift_notice(self) -> str | None:
        """One line for the control channel at startup, or ``None`` when this
        host is not behind. Only ``lantern`` is checked: its metadata pins the
        worker exactly, so they move together."""
        mine = lantern.__version__
        newest = self.latest("lantern")
        if compare(mine, newest) != "behind":
            return None
        gap = behind_by(mine, newest)
        gap_text = f", {gap} patch release{'s' if gap != 1 else ''} behind" if gap else ""
        return (
            f"⚠️ this daemon is running lantern {mine}; the latest release is {newest}{gap_text}. "
            f"Upgrading is an operator's step on the host — {self.upgrade_hint()} — then "
            "restart the daemon; it keeps running the code it started with until then."
        )


def start_drift_check(
    probe: VersionProbe, notify: Callable[[str], None] | None = None
) -> threading.Thread:
    """Check for release drift off the startup path and narrate it once.

    A daemon thread for the same reason :meth:`Concierge.warm_up` uses one:
    the network call must not delay the daemon coming up, and nothing should
    wait on it at shutdown. The verdict always reaches the journal; ``notify``
    (the frontend) hears only about drift, so a current daemon starts quietly.
    """

    def run() -> None:
        try:
            installed = probe.installed()
            notice = probe.drift_notice()
            log.info(
                "versions.checked",
                lantern=installed.lantern,
                worker=installed.worker,
                sbx=installed.sbx,
                latest=probe.latest("lantern"),
                behind=notice is not None,
            )
            if notice is not None and notify is not None:
                notify(notice)
        except Exception:
            # Never let a version check take the daemon down with it.
            log.warning("versions.check_failed", exc_info=True)

    thread = threading.Thread(target=run, name="lantern-version-check", daemon=True)
    thread.start()
    return thread
