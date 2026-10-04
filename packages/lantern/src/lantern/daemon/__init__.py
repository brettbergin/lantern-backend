"""``lantern daemon`` — the always-on outer loop around the run engine.

Discovers work (GitHub issues by label), runs each item through a fresh
:class:`~lantern.engine.engine.LoopEngine` that carries it all the way to
a merged pull request, settles the issue on how the run ended, mirrors the
chronology to chat (Discord or Slack), and keeps going. It starts nothing
on its own account: every run traces to a person's act, a schedule a person
created, or a grant an owner wrote — and grants ship empty.
"""

from lantern.daemon.model import DaemonNotice, RunReport, TickResult, WorkItem

__all__ = ["DaemonNotice", "RunReport", "TickResult", "WorkItem"]
