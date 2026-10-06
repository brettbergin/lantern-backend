"""`[attention]`: when a thing that waits on a person is reminded about."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lantern.config import AttentionConfig, Config


class TestDefaults:
    def test_reminds_after_four_hours_then_daily(self) -> None:
        assert Config().attention == AttentionConfig(remind_after_s=14400, remind_every_s=86400)

    def test_zero_switches_reminders_off(self) -> None:
        config = Config.model_validate({"attention": {"remind_after_s": 0}})
        assert config.attention.remind_after_s == 0 and config.attention.enabled is False
        assert Config().attention.enabled is True


class TestBounds:
    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("remind_after_s", 60),
            ("remind_after_s", 299),
            ("remind_after_s", -1),
            ("remind_after_s", 2592001),
            ("remind_every_s", 0),
            ("remind_every_s", 60),
            ("remind_every_s", 299),
            ("remind_every_s", 2592001),
        ],
    )
    def test_a_value_the_tracker_could_not_honour_is_refused_at_load(
        self, key: str, value: int
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            Config.model_validate({"attention": {key: value}})
        message = str(caught.value)
        assert key in message
        # The bound is named, so the operator knows what to write.
        assert "300" in message and "2592000" in message

    @pytest.mark.parametrize("value", [300, 14400, 2592000])
    def test_the_floor_and_the_ceiling_load(self, value: int) -> None:
        config = Config.model_validate(
            {"attention": {"remind_after_s": value, "remind_every_s": value}}
        )
        assert config.attention.remind_after_s == value
        assert config.attention.remind_every_s == value
