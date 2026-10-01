"""The concierge role speaks as Lantern, not as a separate agent."""

from __future__ import annotations

from lantern.api.agents import AGENTS_BY_SLUG, LANTERN_PERSONA


def test_a_mentioned_concierge_stays_lantern() -> None:
    persona = AGENTS_BY_SLUG["concierge"].persona
    assert persona.startswith(LANTERN_PERSONA)
    assert "You are Lantern" in persona
    assert "@concierge" in persona and "@lantern" in persona
    assert "Concierge**" not in persona and "lantern's **" not in persona


def test_other_roles_keep_their_collaboration_persona() -> None:
    persona = AGENTS_BY_SLUG["builder"].persona
    assert "You are lantern's **Builder**" in persona
    assert "@builder" in persona
    assert "You are Lantern" not in persona
