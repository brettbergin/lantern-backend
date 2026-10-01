"""The typed console app a widget or screen belongs to — resolved lazily,
so widget modules never import the app module at import time."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lantern.tui.app import LanternTui


def console_of(node: Any) -> LanternTui:
    from lantern.tui.app import LanternTui

    app = node.app
    assert isinstance(app, LanternTui)
    return app


__all__ = ["console_of"]
