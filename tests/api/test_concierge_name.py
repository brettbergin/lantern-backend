"""The product agent answers to the name an operator gives it.

A ``[[agents]]`` entry for ``concierge`` may rename the agent that speaks as
the product and choose the ``@`` aliases people address it by. The slug,
the chat session keys and everything stored stay what they were; the name
is what the agent says it is, what the agent directory lists, what the
preference prompts and refusals call it, and what a signed-out client is
told before anyone signs in. With no such entry nothing changes.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lantern.agents.registry import ConfigAgentRegistry
from lantern.api.agents import LANTERN_MENTIONED, LANTERN_PERSONA
from lantern.config import Config
from tests.api.conftest import Api, build
from tests.api.test_collaboration import FakeConcierge, bearer, register

RENAMED: dict[str, Any] = {
    "agents": [{"slug": "concierge", "name": "Robin", "aliases": ["robin", "lantern"]}]
}


@pytest.fixture
def renamed(tmp_path: Path) -> Iterator[Api]:
    # Its own home, so a test may serve it beside the shipped ``api``.
    home = tmp_path / "renamed"
    home.mkdir()
    built = build(home, config=RENAMED)
    with built.client:
        yield built
    built.ctx.close()


def _wait_for_calls(concierge: FakeConcierge, count: int = 1) -> None:
    deadline = time.monotonic() + 5
    while len(concierge.calls) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(concierge.calls) >= count


# -- the registry and the persona ---------------------------------------------------


def test_the_shipped_persona_is_unchanged_without_a_rename() -> None:
    concierge = ConfigAgentRegistry(Config()).get("concierge")
    assert concierge is not None
    assert concierge.chat_persona() == LANTERN_PERSONA + LANTERN_MENTIONED
    assert "You are Lantern," in LANTERN_PERSONA
    assert "`@concierge` (or `@lantern`)" in LANTERN_MENTIONED


def test_a_renamed_concierge_speaks_under_its_name_and_aliases() -> None:
    registry = ConfigAgentRegistry(Config.model_validate(RENAMED))
    concierge = registry.get("concierge")
    assert concierge is not None
    assert concierge.spec.name == "Robin"
    for selector in ("robin", "ROBIN", "lantern", "concierge"):
        found = registry.get(selector)
        assert found is not None and found.slug == "concierge", selector

    persona = concierge.chat_persona()
    assert "You are Robin, a concise personal assistant" in persona
    assert "`@concierge` (or `@robin` or `@lantern`)" in persona
    assert "You are still Robin" in persona
    assert "Lantern" not in persona


def test_a_rename_alone_keeps_the_shipped_alias() -> None:
    config = Config.model_validate({"agents": [{"slug": "concierge", "name": "Robin"}]})
    concierge = ConfigAgentRegistry(config).get("concierge")
    assert concierge is not None
    # The shipped alias stays unless the entry names others.
    assert concierge.spec.aliases == ["lantern"]


def test_other_roles_respond_within_the_configured_name() -> None:
    planner = ConfigAgentRegistry(Config.model_validate(RENAMED)).get("planner")
    assert planner is not None
    assert "responding in Robin as `@planner`" in planner.chat_persona("Robin")
    assert "responding in Lantern as `@planner`" in planner.chat_persona()


# -- the API ------------------------------------------------------------------------


def test_the_directory_lists_the_configured_name(renamed: Api) -> None:
    headers = bearer(register(renamed))

    listed = renamed.client.get("/v1/agents", headers=headers).json()
    concierge = next(agent for agent in listed if agent["slug"] == "concierge")
    assert concierge["name"] == "Robin"
    assert concierge["aliases"] == ["robin", "lantern"]
    assert "You are Robin" in concierge["system_prompt"]
    planner = next(agent for agent in listed if agent["slug"] == "planner")
    assert "responding in Robin as `@planner`" in planner["system_prompt"]

    one = renamed.client.get("/v1/agents/concierge", headers=headers).json()
    assert one == concierge
    assert renamed.client.get("/v1/agents/robin", headers=headers).json()["slug"] == "concierge"


def test_the_directory_keeps_its_old_listing_without_a_rename(api: Api) -> None:
    headers = bearer(register(api))
    concierge = api.client.get("/v1/agents/concierge", headers=headers).json()
    assert concierge["name"] == "Concierge"
    assert concierge["aliases"] == ["lantern"]
    assert "You are Lantern" in concierge["system_prompt"]


def test_a_signed_out_client_learns_the_name(renamed: Api, api: Api) -> None:
    assert renamed.client.get("/v1/auth/providers").json()["assistant_name"] == "Robin"
    assert api.client.get("/v1/auth/providers").json()["assistant_name"] == "Lantern"


def test_preference_prompts_name_the_configured_assistant(renamed: Api, api: Api) -> None:
    def descriptions(served: Api, headers: dict[str, str]) -> dict[str, str]:
        found = served.client.get("/v1/prompts/definitions", headers=headers).json()
        return {item["name"]: item["description"] for item in found}

    robin = descriptions(renamed, bearer(register(renamed)))
    assert robin["personality"] == "How would you like Robin to communicate with you?"
    assert robin["style"] == "How detailed should Robin's responses be? Any tone preferences?"
    assert not any("Lantern" in text for text in robin.values())

    lantern = descriptions(api, bearer(register(api)))
    assert lantern["personality"] == "How would you like Lantern to communicate with you?"


def test_a_mention_by_a_configured_alias_reaches_the_concierge(renamed: Api) -> None:
    concierge = FakeConcierge()
    renamed.ctx.concierge = concierge
    headers = bearer(register(renamed))
    channel_id = renamed.client.post("/v1/channels", json={}, headers=headers).json()["id"]

    response = renamed.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "@robin what is running?"},
    )
    assert response.status_code == 202, response.text

    _wait_for_calls(concierge)
    (call,) = concierge.calls
    # Stored identifiers keep the shipped names: the session and the author.
    assert call["session_key"] == f"{channel_id}:lantern"
    assert call["agent_slug"] == "concierge"
    assert "You are Robin" in call["persona"]


def test_refusals_name_the_configured_assistant(renamed: Api) -> None:
    headers = bearer(register(renamed))
    channel_id = renamed.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    renamed.ctx.concierge = FakeConcierge()

    conflict = renamed.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "fix it", "intent": "code", "target_slugs": ["planner"]},
    )
    assert conflict.status_code == 422, conflict.text
    assert "coordinated by Robin" in conflict.json()["detail"]

    machine = renamed.client.get("/v1/users/me", headers=renamed.bearer())
    assert machine.status_code == 403
    assert machine.json()["code"] == "local_profile_required"
    assert machine.json()["detail"] == "this client is not the local Robin user"
