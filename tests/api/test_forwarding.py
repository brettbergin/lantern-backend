"""Whose forwarded client address the listener believes, and saying so
when a proxy's is ignored: behind a proxy nobody told the listener about,
every client is the proxy's address to the sign-in limiter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lantern.api.app import create_app
from lantern.api.forwarding import UNTRUSTED_FORWARDING_KEY
from lantern.api.server import ApiServer
from lantern.cli.doctor import api_forwarding_checks
from lantern.config import ApiConfig
from tests.api.conftest import build


class TestWhoIsBelieved:
    def test_an_unset_list_believes_the_loopback_proxy_on_a_loopback_bind(self) -> None:
        assert ApiConfig().forwarding_proxies == ["127.0.0.1", "::1"]
        assert ApiConfig(bind="::1").forwarding_proxies == ["127.0.0.1", "::1"]
        assert ApiConfig(bind="localhost").forwarding_proxies == ["127.0.0.1", "::1"]

    def test_a_wider_bind_believes_nobody_unless_told(self) -> None:
        assert ApiConfig(bind="0.0.0.0").forwarding_proxies == []  # nosec B104 - a value under test
        assert ApiConfig(bind="10.0.0.5").forwarding_proxies == []

    def test_a_set_list_is_taken_as_written(self) -> None:
        assert ApiConfig(trusted_proxies=[]).forwarding_proxies == []
        assert ApiConfig(trusted_proxies=["10.0.0.0/8"]).forwarding_proxies == ["10.0.0.0/8"]
        loaded = ApiConfig.model_validate({"trusted_proxies": []})
        assert loaded.model_copy(update={"port": 0}).forwarding_proxies == []

    def test_the_listener_reads_forwarded_headers_from_whom_it_believes(
        self, tmp_path: Path
    ) -> None:
        api = build(tmp_path)
        try:
            server = ApiServer(create_app(api.ctx), api.ctx.config.api, ctx=api.ctx)
            assert server._uv.config.proxy_headers is True
            assert server._uv.config.forwarded_allow_ips == ["127.0.0.1", "::1"]
            closed = ApiConfig.model_validate(
                {**api.ctx.config.api.model_dump(), "trusted_proxies": []}
            )
            server = ApiServer(create_app(api.ctx), closed, ctx=api.ctx)
            assert server._uv.config.proxy_headers is False
        finally:
            api.ctx.close()


def _note(api_dstore: object) -> dict[str, object] | None:
    raw = api_dstore.get_value(UNTRUSTED_FORWARDING_KEY)  # type: ignore[attr-defined]
    return None if raw is None else dict(json.loads(raw))


class TestIgnoredProxy:
    def test_a_proxied_request_nobody_believes_is_noted_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = build(tmp_path, trusted_proxies=[])
        try:
            client = TestClient(create_app(api.ctx), client=("127.0.0.1", 50000))
            with client:
                client.get("/health/live", headers={"X-Forwarded-For": "203.0.113.9"})
                client.get("/health/live", headers={"X-Forwarded-For": "203.0.113.10"})
            note = _note(api.loop.dstore)
            assert note is not None and note["peer"] == "127.0.0.1"
            config = api.ctx.config.model_copy(
                update={"api": ApiConfig.model_validate({"enabled": True, "trusted_proxies": []})}
            )
            rows = api_forwarding_checks(config)
            assert [(r.name, r.ok, r.hard) for r in rows] == [
                ("api client addresses", False, False)
            ]
            assert '`[api] trusted_proxies = ["127.0.0.1"]`' in rows[0].detail
        finally:
            api.ctx.close()

    def test_a_direct_or_believed_request_is_not_noted(self, tmp_path: Path) -> None:
        api = build(tmp_path, trusted_proxies=[])
        try:
            local = TestClient(create_app(api.ctx), client=("127.0.0.1", 50000))
            remote = TestClient(create_app(api.ctx), client=("93.184.216.34", 50000))
            with local, remote:
                local.get("/health/live")
                remote.get("/health/live", headers={"X-Forwarded-For": "198.51.100.1"})
            assert _note(api.loop.dstore) is None
        finally:
            api.ctx.close()
        believed = build(tmp_path / "believed")
        try:
            client = TestClient(create_app(believed.ctx), client=("127.0.0.1", 50000))
            with client:
                client.get("/health/live", headers={"X-Forwarded-For": "203.0.113.9"})
            assert _note(believed.loop.dstore) is None
        finally:
            believed.ctx.close()

    def test_doctor_is_silent_once_the_configuration_believes_a_proxy(self, tmp_path: Path) -> None:
        api = build(tmp_path, trusted_proxies=[])
        try:
            api.loop.dstore.set_value(
                UNTRUSTED_FORWARDING_KEY, json.dumps({"peer": "127.0.0.1", "seen_at": 1.0})
            )
            fixed = api.ctx.config.model_copy(
                update={"api": ApiConfig.model_validate({"enabled": True})}
            )
            assert api_forwarding_checks(fixed) == []
        finally:
            api.ctx.close()
