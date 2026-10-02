"""A screen's worker reports onto the screen's widgets — if the screen still
has them. A mode switch retires a screen while its load is in flight, and
the late result found no ``#resolved`` (or ``#page``) to fill: a
``NoMatches`` out of a worker, and a console that logged a crash for a
screen nobody was looking at."""

from __future__ import annotations

from textual.css.query import NoMatches

from lantern.paths import LanternHome
from lantern.tui.screens.base import ConsoleScreen
from lantern.tui.screens.overview import OverviewScreen
from tests.unit.tui.conftest import drive, make_app, until


def test_a_result_for_a_screen_that_is_not_mounted_is_dropped() -> None:
    painted: list[str] = []
    screen = ConsoleScreen()
    screen.apply_from_worker(painted.append, "late")
    assert painted == []


def test_a_mounted_screen_paints_and_a_missing_widget_is_not_an_error(seeded: LanternHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(120, 30)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, OverviewScreen))
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            painted: list[str] = []
            screen.apply_from_worker(painted.append, "now")
            assert painted == ["now"]

            def gone(_: str) -> None:
                screen.query_one("#not-there")

            screen.apply_from_worker(gone, "late")  # raises NoMatches inside; swallowed
            try:
                screen.query_one("#not-there")
            except NoMatches:
                pass
            else:
                raise AssertionError("the widget should not exist")

    drive(scenario)
