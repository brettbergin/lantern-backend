"""A bridge card (EmbedSpec) as a bordered panel."""

from __future__ import annotations

from textual.widgets import Static

from lantern.daemon.discord_format import EmbedSpec
from lantern.tui.format import card


class CardWidget(Static):
    def show(self, spec: EmbedSpec) -> None:
        self.update(card(spec))
