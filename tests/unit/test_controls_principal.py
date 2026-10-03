"""A principal is who is asking; the attribution string is derived from it,
never the other way round."""

from __future__ import annotations

import pytest

from lantern.daemon.controls import ALL_CAPABILITIES, CAPABILITIES, WORKSPACE_ID, Principal
from lantern.daemon.controls.principal import ROLE_CAPABILITIES
from lantern.daemon.controls.results import ControlError
from lantern.daemon.controls.service import require


class TestTrusted:
    @pytest.mark.parametrize(
        ("by", "via"),
        [
            ("brett via lantern daemon ctl", "ctl"),
            ("discord user `brett`", "discord"),
            ("slack user `U1`", "slack"),
            ("mattermost user `m`", "mattermost"),
            ("brett via lantern tui", "local"),
            ("ops", "concierge"),
        ],
    )
    def test_keeps_the_legacy_attribution_byte_for_byte(self, by: str, via: str) -> None:
        principal = Principal.trusted(by, via)
        assert principal.attribution() == by
        assert principal.via == via
        assert principal.kind == "operator"
        assert principal.capabilities == ALL_CAPABILITIES
        assert principal.can("policy:manage")
        assert principal.workspace_id == WORKSPACE_ID

    def test_a_missing_attribution_stays_missing(self) -> None:
        """The loop's own ``by or "operator"`` fallbacks must still fire:
        a trusted surface with no name is not renamed here."""
        principal = Principal.trusted(None, "ctl")
        assert principal.attribution() is None
        assert principal.id == "ctl"

    def test_every_capability_is_granted(self) -> None:
        principal = Principal.trusted("x", "ctl")
        assert all(principal.can(cap) for cap in CAPABILITIES)


class TestScoped:
    def test_a_client_holds_only_what_it_was_granted(self) -> None:
        client = Principal(
            kind="client",
            id="cli_1",
            display="reporter",
            via="api",
            capabilities=frozenset({"runs:read"}),
        )
        assert client.can("runs:read")
        assert not client.can("runs:control")
        require(client, "runs:read")
        with pytest.raises(ControlError) as excinfo:
            require(client, "runs:control")
        assert excinfo.value.code == "forbidden"
        assert "runs:control" in excinfo.value.message
        assert excinfo.value.detail == {"capability": "runs:control"}

    def test_audit_fields_carry_no_capabilities(self) -> None:
        """The audit line says who; what they may do is policy, not record."""
        client = Principal(kind="client", id="cli_1", display=None, via="api")
        assert client.audit() == {
            "kind": "client",
            "id": "cli_1",
            "display": None,
            "via": "api",
            "workspace_id": "local",
        }

    def test_system_principal_has_no_attribution(self) -> None:
        assert Principal.system("schedule").attribution() is None


class TestPolicyManage:
    """Editing the standing rules that let agents take decisions is the
    owner's alone: no other role holds it, and an agent principal never
    does, whoever it is working for."""

    def test_it_is_a_capability(self) -> None:
        assert "policy:manage" in CAPABILITIES

    def test_an_owner_holds_it(self) -> None:
        assert ROLE_CAPABILITIES["owner"] == ALL_CAPABILITIES
        owner = Principal(
            kind="client",
            id="usr_owner",
            display="olive",
            via="api",
            capabilities=ROLE_CAPABILITIES["owner"],
        )
        assert owner.can("policy:manage")
        require(owner, "policy:manage")

    @pytest.mark.parametrize("role", ["admin", "member"])
    def test_no_other_role_holds_it(self, role: str) -> None:
        person = Principal(
            kind="client",
            id=f"usr_{role}",
            display=role,
            via="api",
            capabilities=ROLE_CAPABILITIES[role],  # type: ignore[index]
        )
        assert not person.can("policy:manage")
        with pytest.raises(ControlError) as excinfo:
            require(person, "policy:manage")
        assert excinfo.value.code == "forbidden"
        assert excinfo.value.detail == {"capability": "policy:manage"}

    def test_an_admin_holds_everything_but_credentials_and_policy(self) -> None:
        assert ROLE_CAPABILITIES["admin"] == ALL_CAPABILITIES - {
            "credentials:manage",
            "policy:manage",
        }

    @pytest.mark.parametrize("on_behalf_of", [None, "olive", "the workspace owner"])
    def test_an_agent_never_holds_it(self, on_behalf_of: str | None) -> None:
        """Working for an owner lends an agent nothing: the name rides in the
        attribution alone."""
        agent = Principal.for_agent("planner", on_behalf_of)
        assert agent.capabilities == frozenset({"items:create"})
        assert not agent.can("policy:manage")
        with pytest.raises(ControlError):
            require(agent, "policy:manage")

    def test_a_trusted_operator_holds_it(self) -> None:
        operator = Principal.trusted("brett via lantern daemon ctl", "ctl")
        assert operator.can("policy:manage")
        require(operator, "policy:manage")
