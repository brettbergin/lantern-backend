"""lantern — agentic loop orchestration on Docker Sandboxes (sbx).

Every lantern operation runs as a pair of isolated microVM sandboxes: an agent
sandbox holding only ``COPILOT_GITHUB_TOKEN`` for the GitHub Copilot SDK
agentic layer, and a GitHub-ops sandbox holding only ``GH_TOKEN`` for
user-facing GitHub interactions. The balanced network policy is the default.
"""

try:
    # written at build time by hatch-vcs (see pyproject [tool.hatch.build.hooks.vcs])
    from lantern._version import __version__
except ImportError:  # pragma: no cover - raw source tree that was never built
    try:
        from importlib.metadata import version as _pkg_version

        __version__ = _pkg_version("lantern-backend")
    except Exception:
        __version__ = "0.0.0"

from lantern.config import Budgets, Config, load_config
from lantern.engine import LoopEngine, RunResult, run_outcome
from lantern.events import Event, EventBus, Hook

__all__ = [
    "Budgets",
    "Config",
    "Event",
    "EventBus",
    "Hook",
    "LoopEngine",
    "RunResult",
    "__version__",
    "load_config",
    "run_outcome",
]
