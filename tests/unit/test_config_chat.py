"""[chat] backend selection: explicit, inferred, ambiguous, dangling — and
the Slack section's own validation."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import get_args

import pytest

from lantern.chatservices import CHAT_SERVICES, service_named
from lantern.config import (
    CHAT_BACKENDS,
    ChatBackend,
    ChatBridgeConfig,
    Config,
    DiscordConfig,
    SlackConfig,
    load_config,
)
from lantern.errors import ConfigError


class TestBackendSelection:
    def test_headless_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.chat.backend is None
        assert config.chat_backend is None
        assert config.chat_settings is None
        assert config.slack.enabled is False and config.discord.enabled is False

    def test_inferred_from_the_one_configured_section(self) -> None:
        discord = Config.model_validate({"discord": {"channel_id": 42}})
        assert discord.chat_backend == "discord"
        assert isinstance(discord.chat_settings, DiscordConfig)
        assert discord.chat_settings.channel_ref == "42"
        slack = Config.model_validate({"slack": {"channel_id": "C0123ABCDEF"}})
        assert slack.chat_backend == "slack"
        assert isinstance(slack.chat_settings, SlackConfig)
        assert slack.chat_settings.channel_ref == "C0123ABCDEF"

    def test_explicit_backend_wins_over_the_other_section(self) -> None:
        config = Config.model_validate(
            {
                "chat": {"backend": "slack"},
                "discord": {"channel_id": 42},
                "slack": {"channel_id": "C0123ABCDEF"},
            }
        )
        assert config.chat_backend == "slack"
        assert config.chat_settings is config.slack

    def test_both_sections_without_a_choice_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "lantern.toml").write_text(
            '[discord]\nchannel_id = 42\n[slack]\nchannel_id = "C0123ABCDEF"\n'
        )
        with pytest.raises(ConfigError, match=r"both \[discord\] and \[slack\].*\[chat\] backend"):
            load_config(cwd=tmp_path, env={})

    def test_named_backend_without_its_section_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "lantern.toml").write_text('[chat]\nbackend = "slack"\n')
        with pytest.raises(ConfigError, match=r"backend = \"slack\" but \[slack\] channel_id"):
            load_config(cwd=tmp_path, env={})
        (tmp_path / "lantern.toml").write_text(
            '[chat]\nbackend = "discord"\n[slack]\nchannel_id = "C0123ABCDEF"\n'
        )
        with pytest.raises(ConfigError, match=r"\[discord\] channel_id is not set"):
            load_config(cwd=tmp_path, env={})

    def test_unknown_backend_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "lantern.toml").write_text('[chat]\nbackend = "irc"\n')
        with pytest.raises(ConfigError, match="backend"):
            load_config(cwd=tmp_path, env={})

    def test_env_layer_reaches_both_sections(self, tmp_path: Path) -> None:
        config = load_config(
            cwd=tmp_path,
            env={
                "LANTERN_CHAT__BACKEND": "slack",
                "LANTERN_SLACK__CHANNEL_ID": "C0123ABCDEF",
                "LANTERN_SLACK__CHRONOLOGY_LEVEL": "quiet",
                "LANTERN_DISCORD__CHANNEL_ID": "42",
            },
        )
        assert config.chat_backend == "slack"
        assert config.slack.chronology_level == "quiet"
        assert config.discord.enabled  # still parsed, just not the active one

    def test_model_copy_with_a_channel_is_honoured_without_revalidation(self) -> None:
        """The CLI's --slack-channel/--discord-channel overrides copy the
        model; the backend is a property so the copy answers correctly."""
        config = Config()
        assert config.chat_backend is None
        slack = config.model_copy(update={"slack": SlackConfig(channel_id="C0123ABCDEF")})
        assert slack.chat_backend == "slack"


class TestSlackSection:
    def test_shared_knobs_have_the_discord_defaults(self) -> None:
        slack, discord = SlackConfig(), DiscordConfig()
        for name in (
            "command_prefix",
            "thread_per_run",
            "chronology_level",
            "max_message_chars",
            "embeds",
            "status_line",
            "tool_batch_lines",
            "tool_output_lines",
            "tool_fail_output_lines",
            "max_attachment_bytes",
        ):
            assert getattr(slack, name) == getattr(discord, name), name

    def test_channel_id_must_be_an_id_not_a_name(self) -> None:
        with pytest.raises(ValueError, match="channel's id"):
            SlackConfig(channel_id="#lantern")
        with pytest.raises(ValueError, match="channel's id"):
            SlackConfig(channel_id="general")
        # a user id or a DM is well-formed but not a channel
        with pytest.raises(ValueError, match="not a user id"):
            SlackConfig(channel_id="U0123ABCDEF")
        with pytest.raises(ValueError, match="not a user id"):
            SlackConfig(channel_id="D0123ABCDEF")
        assert SlackConfig(channel_id="  C0123ABCDEF ").channel_id == "C0123ABCDEF"
        assert SlackConfig(channel_id="G0123ABCDEF").enabled
        assert SlackConfig(channel_id="").channel_id is None


class TestServiceDescriptors:
    """The descriptor set and the ``ChatBackend`` Literal name the same
    services, and every lookup that used to branch on "discord, else slack"
    resolves through it (#930)."""

    def test_literal_and_descriptors_name_the_same_services(self) -> None:
        assert tuple(service.name for service in CHAT_SERVICES) == CHAT_BACKENDS
        assert set(get_args(ChatBackend)) == {service.name for service in CHAT_SERVICES}

    def test_every_service_resolves_its_config_section(self) -> None:
        config = Config.model_validate({})
        for service in CHAT_SERVICES:
            section = config.chat_section(service.name)
            assert section is getattr(config, service.section)
            assert isinstance(section, ChatBridgeConfig)

    def test_every_service_names_an_importable_bridge(self) -> None:
        for service in CHAT_SERVICES:
            module = import_module(service.module)
            bridge = getattr(module, service.bridge_class)
            assert bridge.backend == service.name
            assert bridge.label == service.label

    def test_unknown_backend_names_the_known_ones(self) -> None:
        known = ", ".join(service.name for service in CHAT_SERVICES)
        with pytest.raises(ValueError, match=rf"unknown chat backend 'irc' \(known: {known}\)"):
            service_named("irc")

    def test_missing_extra_detail_names_the_sdk_and_its_packages(self) -> None:
        # The third-party packages themselves, never `lantern-backend[extra]`: that
        # would send pip to a package index for our own name.
        discord = service_named("discord").missing_extra_detail
        assert discord.startswith("discord.py missing (install 'discord")
        assert discord.endswith("into the venv lantern runs from)")
        slack = service_named("slack").missing_extra_detail
        assert slack.startswith("slack_sdk missing (install ")
        assert "'slack-sdk>=" in slack and "'aiohttp>=" in slack
        for service in CHAT_SERVICES:
            assert "lantern-backend[" not in service.missing_extra_detail


class TestExtraInstallHint:
    def test_names_the_extras_packages_from_the_installed_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lantern import releases

        requires = [
            "pydantic>=2.7",
            "lantern-worker==1.2.3",
            "discord-py>=2.3; extra == 'discord'",
            'aiohttp>=3.9; extra == "slack"',
            "slack-sdk>=3.44.1; extra == 'slack'",
            "aiohttp>=3.9; extra == 'mattermost'",
        ]
        monkeypatch.setattr(releases.metadata, "requires", lambda _name: requires)
        assert releases.extra_install_hint("slack", "slack-sdk") == (
            "install 'aiohttp>=3.9' 'slack-sdk>=3.44.1' into the venv lantern runs from"
        )
        assert "discord-py>=2.3" in releases.extra_install_hint("discord", "discord.py")

    def test_falls_back_to_the_named_package_without_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from importlib import metadata

        from lantern import releases

        def missing(_name: str) -> list[str]:
            raise metadata.PackageNotFoundError

        monkeypatch.setattr(releases.metadata, "requires", missing)
        assert releases.extra_install_hint("discord", "discord.py") == (
            "install 'discord.py' into the venv lantern runs from"
        )
