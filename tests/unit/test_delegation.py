"""What an agent may decide: the closed list, a grant's conditions, and the
judge that answers allow, deny or escalate from host-supplied facts."""

from __future__ import annotations

from typing import Any

import pytest

from lantern.daemon.controls.delegation import (
    CONDITION_KEYS,
    DELEGABLE_ACTIONS,
    Conditions,
    Grant,
    GrantInvalid,
    check_action,
    check_conditions,
    check_daily_limit,
    decide,
    parse_conditions,
)


def grant(grant_id: str = "grant_a", **fields: Any) -> Grant:
    values: dict[str, Any] = {
        "id": grant_id,
        "agent_slug": "critic",
        "action": "plan.approve",
        "conditions": Conditions(),
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    conditions = fields.pop("conditions", None)
    if conditions is not None:
        values["conditions"] = Conditions(**conditions)
    values.update(fields)
    return Grant(**values)


#: The facts a host supplies for an approval nobody could object to.
APPROVAL: dict[str, object] = {
    "repository": "o/r",
    "level": "epic",
    "child_count": 4,
    "proposer": "planner",
    "review_verdict": "approve",
}
RETRY: dict[str, object] = {"repository": "o/r", "failure_cause": "timeout", "retries": 0}


class TestTheClosedList:
    def test_the_delegable_actions_are_exactly_these(self) -> None:
        assert DELEGABLE_ACTIONS == (
            "plan.propose",
            "plan.breakdown",
            "plan.approve",
            "plan.publish",
            "plan.run",
            "plan.run.retry",
            "item.retry",
            "run.grant_rounds",
        )
        assert set(CONDITION_KEYS) == set(DELEGABLE_ACTIONS)

    @pytest.mark.parametrize(
        "action",
        [
            "grant.create",
            "grant.update",
            "grant.delete",
            "daemon.stop",
            "daemon.pause",
            "gate.approve",
            "schedule.add",
            "repo.add",
            "config.set",
            "credentials.rotate",
            "",
        ],
    )
    def test_nothing_else_can_be_granted(self, action: str) -> None:
        with pytest.raises(GrantInvalid) as refused:
            check_action(action)
        assert refused.value.field == "action"
        assert "cannot be delegated" in str(refused.value)

    @pytest.mark.parametrize("action", DELEGABLE_ACTIONS)
    def test_every_listed_action_is_accepted(self, action: str) -> None:
        check_action(action)


class TestConditions:
    #: The action x key table, as the module and the docs state it.
    TABLE: dict[str, set[str]] = {  # noqa: RUF012 - a constant table
        "plan.propose": {"repositories", "levels"},
        "plan.breakdown": {"repositories", "levels"},
        "plan.approve": {"repositories", "levels", "max_children", "require_review"},
        "plan.publish": {"repositories", "levels", "max_children", "require_review"},
        "plan.run": {"repositories", "max_children"},
        "plan.run.retry": {"repositories", "causes", "max_retries"},
        "item.retry": {"repositories", "causes", "max_retries"},
        "run.grant_rounds": {"repositories", "causes", "max_retries"},
    }
    VALUES: dict[str, object] = {  # noqa: RUF012 - a constant table
        "repositories": ["o/r"],
        "levels": ["epic"],
        "max_children": 5,
        "require_review": True,
        "causes": ["timeout"],
        "max_retries": 2,
    }

    def test_the_table_is_the_one_documented(self) -> None:
        assert {action: set(keys) for action, keys in CONDITION_KEYS.items()} == self.TABLE

    @pytest.mark.parametrize("action", DELEGABLE_ACTIONS)
    def test_each_action_takes_its_keys_and_refuses_the_rest(self, action: str) -> None:
        for key, value in self.VALUES.items():
            if key in self.TABLE[action]:
                parsed = parse_conditions(action, {key: value})
                assert parsed.set_keys() == (key,)
                continue
            with pytest.raises(GrantInvalid) as refused:
                parse_conditions(action, {key: value})
            assert refused.value.field == f"conditions.{key}"
            assert key in str(refused.value) and action in str(refused.value)

    def test_no_conditions_is_valid_for_every_action(self) -> None:
        for action in DELEGABLE_ACTIONS:
            assert parse_conditions(action, {}) == Conditions()
            assert parse_conditions(action, None) == Conditions()

    def test_a_key_left_at_its_default_is_not_a_key_that_was_set(self) -> None:
        """A client that sends the whole object with nulls is not refused."""
        parsed = parse_conditions(
            "plan.run", {"levels": None, "require_review": False, "causes": None}
        )
        assert parsed.set_keys() == ()

    def test_an_unknown_key_is_refused_by_name(self) -> None:
        with pytest.raises(GrantInvalid) as refused:
            parse_conditions("plan.approve", {"expression": "child_count < 3"})
        assert refused.value.field == "conditions.expression"

    @pytest.mark.parametrize(
        ("action", "raw", "field"),
        [
            ("plan.approve", {"repositories": []}, "conditions.repositories"),
            ("plan.approve", {"repositories": ["  "]}, "conditions.repositories"),
            ("plan.approve", {"repositories": "o/r"}, "conditions.repositories"),
            ("plan.approve", {"levels": []}, "conditions.levels"),
            ("plan.approve", {"levels": ["story"]}, "conditions.levels"),
            ("plan.approve", {"max_children": 0}, "conditions.max_children"),
            ("plan.approve", {"max_children": "many"}, "conditions.max_children"),
            ("plan.approve", {"require_review": "yes please"}, "conditions.require_review"),
            ("item.retry", {"causes": []}, "conditions.causes"),
            ("item.retry", {"causes": ["unknown"]}, "conditions.causes"),
            ("item.retry", {"max_retries": 0}, "conditions.max_retries"),
            ("item.retry", {"max_retries": -1}, "conditions.max_retries"),
        ],
    )
    def test_a_value_that_makes_no_sense_names_its_key(
        self, action: str, raw: dict[str, object], field: str
    ) -> None:
        with pytest.raises(GrantInvalid) as refused:
            parse_conditions(action, raw)
        assert refused.value.field == field

    def test_lists_are_trimmed_and_deduplicated_in_order(self) -> None:
        parsed = parse_conditions(
            "plan.approve", {"repositories": [" o/r ", "o/two", "O/R"], "levels": ["task", "task"]}
        )
        assert parsed.repositories == ("o/r", "o/two")
        assert parsed.levels == ("task",)

    def test_conditions_already_parsed_are_checked_against_the_action(self) -> None:
        check_conditions("plan.approve", Conditions(max_children=3))
        with pytest.raises(GrantInvalid) as refused:
            check_conditions("plan.propose", Conditions(max_children=3))
        assert refused.value.field == "conditions.max_children"

    @pytest.mark.parametrize("limit", [0, -1, True, 1.5, "3"])
    def test_a_daily_limit_is_positive_or_absent(self, limit: object) -> None:
        with pytest.raises(GrantInvalid) as refused:
            check_daily_limit(limit)
        assert refused.value.field == "daily_limit"

    def test_a_positive_or_absent_daily_limit_is_accepted(self) -> None:
        check_daily_limit(None)
        check_daily_limit(1)
        check_daily_limit(500)


def judge(
    grants: list[Grant],
    *,
    agent: str = "critic",
    action: str = "plan.approve",
    attrs: dict[str, object] | None = None,
    used: dict[str, int] | None = None,
) -> tuple[str, str | None, str]:
    decision = decide(
        grants,
        agent_slug=agent,
        action=action,
        attrs=APPROVAL if attrs is None else attrs,
        used_today=used or {},
    )
    return decision.outcome, decision.grant_id, decision.reason


def without(attrs: dict[str, object], *keys: str, **changed: object) -> dict[str, object]:
    return {**{k: v for k, v in attrs.items() if k not in keys}, **changed}


class TestDeny:
    @pytest.mark.parametrize("action", ["grant.update", "daemon.stop", "gate.approve", ""])
    def test_an_action_outside_the_closed_list_is_denied_whatever_the_grants_say(
        self, action: str
    ) -> None:
        # A row that names it (written around the validation) changes nothing.
        rogue = grant(action=action)
        outcome, grant_id, reason = judge([rogue], action=action)
        assert (outcome, grant_id) == ("deny", None)
        assert "cannot be delegated" in reason

    @pytest.mark.parametrize("proposer", ["critic", "agent:critic", "Critic", " agent:CRITIC "])
    def test_an_agent_never_approves_what_it_proposed(self, proposer: str) -> None:
        outcome, grant_id, reason = judge([grant()], attrs=without(APPROVAL, proposer=proposer))
        assert (outcome, grant_id) == ("deny", None)
        assert "critic" in reason and "proposed" in reason

    def test_self_approval_is_denied_even_with_no_grant_at_all(self) -> None:
        outcome, _, _ = judge([], attrs=without(APPROVAL, proposer="critic"))
        assert outcome == "deny"

    def test_the_proposer_only_matters_for_an_approval(self) -> None:
        publish = grant(action="plan.publish")
        outcome, grant_id, _ = judge(
            [publish], action="plan.publish", attrs=without(APPROVAL, proposer="critic")
        )
        assert (outcome, grant_id) == ("allow", "grant_a")


class TestEscalate:
    def test_no_grant_at_all(self) -> None:
        outcome, grant_id, reason = judge([])
        assert (outcome, grant_id) == ("escalate", None)
        assert reason == "no enabled grant lets critic take plan.approve"

    @pytest.mark.parametrize(
        "other",
        [
            {"agent_slug": "planner"},
            {"action": "plan.publish"},
            {"enabled": False},
        ],
    )
    def test_a_grant_for_another_agent_or_action_or_a_disabled_one_does_not_count(
        self, other: dict[str, Any]
    ) -> None:
        outcome, grant_id, reason = judge([grant(**other)])
        assert (outcome, grant_id) == ("escalate", None)
        assert reason == "no enabled grant lets critic take plan.approve"

    @pytest.mark.parametrize(
        ("conditions", "attrs", "names"),
        [
            ({"repositories": ["o/other"]}, APPROVAL, ("repositories", "o/r")),
            ({"levels": ["task"]}, APPROVAL, ("levels", "epic")),
            ({"max_children": 3}, APPROVAL, ("max_children is 3", "child_count is 4")),
            (
                {"require_review": True},
                without(APPROVAL, review_verdict="escalate"),
                ("require_review", "review_verdict is escalate"),
            ),
        ],
    )
    def test_an_unmet_condition_is_named_with_its_value(
        self, conditions: dict[str, Any], attrs: dict[str, object], names: tuple[str, ...]
    ) -> None:
        outcome, grant_id, reason = judge([grant(conditions=conditions)], attrs=attrs)
        assert (outcome, grant_id) == ("escalate", None)
        assert "grant_a" in reason
        for name in names:
            assert name in reason

    @pytest.mark.parametrize(
        ("conditions", "attrs", "names"),
        [
            ({"causes": ["timeout"]}, without(RETRY, failure_cause="oom"), ("causes", "oom")),
            (
                {"max_retries": 2},
                without(RETRY, retries=2),
                ("max_retries is 2", "retries is 2"),
            ),
        ],
    )
    def test_an_unmet_retry_condition_is_named_with_its_value(
        self, conditions: dict[str, Any], attrs: dict[str, object], names: tuple[str, ...]
    ) -> None:
        retry = grant(agent_slug="operator", action="item.retry", conditions=conditions)
        outcome, grant_id, reason = judge(
            [retry], agent="operator", action="item.retry", attrs=attrs
        )
        assert (outcome, grant_id) == ("escalate", None)
        for name in names:
            assert name in reason

    @pytest.mark.parametrize(
        ("conditions", "missing"),
        [
            ({"repositories": ["o/r"]}, "repository"),
            ({"levels": ["epic"]}, "level"),
            ({"max_children": 9}, "child_count"),
            ({"require_review": True}, "review_verdict"),
        ],
    )
    def test_a_missing_fact_a_condition_needs_fails_closed(
        self, conditions: dict[str, Any], missing: str
    ) -> None:
        for attrs in (without(APPROVAL, missing), without(APPROVAL, **{missing: None})):
            outcome, grant_id, reason = judge([grant(conditions=conditions)], attrs=attrs)
            assert (outcome, grant_id) == ("escalate", None)
            assert missing in reason and "could not tell" in reason

    @pytest.mark.parametrize(
        ("conditions", "attr", "value"),
        [
            ({"repositories": ["o/r"]}, "repository", ""),
            ({"repositories": ["o/r"]}, "repository", 7),
            ({"levels": ["epic"]}, "level", "story"),
            ({"max_children": 9}, "child_count", "four"),
            ({"max_children": 9}, "child_count", True),
            ({"max_children": 9}, "child_count", -1),
            ({"require_review": True}, "review_verdict", ""),
        ],
    )
    def test_a_fact_that_cannot_be_read_fails_closed(
        self, conditions: dict[str, Any], attr: str, value: object
    ) -> None:
        outcome, grant_id, reason = judge(
            [grant(conditions=conditions)], attrs=without(APPROVAL, **{attr: value})
        )
        assert (outcome, grant_id) == ("escalate", None)
        assert attr in reason and "could not tell" in reason

    @pytest.mark.parametrize(
        ("conditions", "attr", "value"),
        [
            ({"causes": ["timeout"]}, "failure_cause", None),
            ({"causes": ["timeout"]}, "failure_cause", "unknown"),
            ({"causes": ["timeout"]}, "failure_cause", "Unknown"),
            ({"max_retries": 2}, "retries", None),
            ({"max_retries": 2}, "retries", "one"),
        ],
    )
    def test_an_unknown_cause_or_retry_count_fails_closed(
        self, conditions: dict[str, Any], attr: str, value: object
    ) -> None:
        retry = grant(agent_slug="operator", action="plan.run.retry", conditions=conditions)
        outcome, grant_id, reason = judge(
            [retry],
            agent="operator",
            action="plan.run.retry",
            attrs=without(RETRY, **{attr: value}),
        )
        assert (outcome, grant_id) == ("escalate", None)
        assert attr in reason and "could not tell" in reason

    @pytest.mark.parametrize("proposer", [None, "", 12])
    def test_an_approval_whose_proposer_is_unknown_fails_closed(self, proposer: object) -> None:
        """Without the proposer the judge cannot rule self-approval out."""
        outcome, grant_id, reason = judge([grant()], attrs=without(APPROVAL, proposer=proposer))
        assert (outcome, grant_id) == ("escalate", None)
        assert "proposer" in reason and "could not tell" in reason

    def test_a_spent_daily_limit(self) -> None:
        limited = grant(daily_limit=2)
        outcome, grant_id, reason = judge([limited], used={"grant_a": 2})
        assert (outcome, grant_id) == ("escalate", None)
        assert "grant_a" in reason and "daily_limit of 2" in reason

    def test_every_candidate_that_falls_short_is_named(self) -> None:
        first = grant("grant_a", conditions={"repositories": ["o/other"]})
        second = grant("grant_b", created_at=2.0, daily_limit=1)
        outcome, grant_id, reason = judge([second, first], used={"grant_b": 1})
        assert (outcome, grant_id) == ("escalate", None)
        assert reason.index("grant_a") < reason.index("grant_b")
        assert "repositories" in reason and "daily_limit" in reason


class TestAllow:
    def test_an_unconditional_grant_allows(self) -> None:
        outcome, grant_id, reason = judge([grant()])
        assert (outcome, grant_id) == ("allow", "grant_a")
        assert reason == "grant grant_a lets critic take plan.approve"

    def test_a_grant_with_no_conditions_needs_no_facts(self) -> None:
        publish = grant(action="plan.publish")
        outcome, grant_id, _ = judge([publish], action="plan.publish", attrs={})
        assert (outcome, grant_id) == ("allow", "grant_a")

    def test_every_condition_met(self) -> None:
        full = grant(
            conditions={
                "repositories": ["O/R"],
                "levels": ["epic", "task"],
                "max_children": 4,
                "require_review": True,
            },
            daily_limit=3,
        )
        outcome, grant_id, _ = judge([full], used={"grant_a": 2})
        assert (outcome, grant_id) == ("allow", "grant_a")

    def test_a_retry_within_its_causes_and_count(self) -> None:
        retry = grant(
            agent_slug="operator",
            action="item.retry",
            conditions={"causes": ["Timeout"], "max_retries": 2},
        )
        outcome, grant_id, _ = judge(
            [retry], agent="operator", action="item.retry", attrs=without(RETRY, retries=1)
        )
        assert (outcome, grant_id) == ("allow", "grant_a")

    def test_the_agent_is_matched_whatever_its_case(self) -> None:
        outcome, _, _ = judge([grant()], agent="Critic")
        assert outcome == "allow"

    def test_a_proposer_that_is_someone_else_is_no_obstacle(self) -> None:
        for proposer in ("planner", "agent:planner", "usr_1"):
            outcome, _, _ = judge([grant()], attrs=without(APPROVAL, proposer=proposer))
            assert outcome == "allow"

    def test_the_oldest_grant_that_allows_is_the_one_named(self) -> None:
        """Several may allow: the pick is by creation time, then id, and
        never by the order they were handed in."""
        older = grant("grant_z", created_at=1.0)
        newer = grant("grant_a", created_at=2.0)
        twin = grant("grant_b", created_at=1.0)
        for grants in ([newer, older, twin], [twin, newer, older], [older, twin, newer]):
            outcome, grant_id, _ = judge(grants)
            assert (outcome, grant_id) == ("allow", "grant_b")

    def test_a_spent_or_unmet_grant_is_passed_over_for_one_that_allows(self) -> None:
        spent = grant("grant_a", created_at=1.0, daily_limit=1)
        narrow = grant("grant_b", created_at=2.0, conditions={"levels": ["task"]})
        open_ = grant("grant_c", created_at=3.0)
        outcome, grant_id, _ = judge([spent, narrow, open_], used={"grant_a": 1})
        assert (outcome, grant_id) == ("allow", "grant_c")

    def test_an_unlimited_grant_is_never_spent(self) -> None:
        outcome, _, _ = judge([grant(daily_limit=None)], used={"grant_a": 10_000})
        assert outcome == "allow"
