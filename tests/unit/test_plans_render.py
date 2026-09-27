"""A plan node rendered as its issue body (#2341).

The body carries the node's sections under headings a person reads on the
forge and a run's decompose reads back, then the hidden ``sbx-plan``
marker that keeps publishing idempotent.
"""

from __future__ import annotations

from typing import Any

from sbxloop.plans.model import ForgeRef, PlanNode
from sbxloop.plans.render import issue_reference, marked, marker, markers, render_body


def _task(**fields: Any) -> PlanNode:
    base: dict[str, Any] = {
        "id": "node_t",
        "plan_id": "plan_p",
        "parent_id": "node_e",
        "position": 0,
        "level": "task",
        "repository": "o/r",
        "state": "approved",
        "origin": "person",
        "title": "Store plans",
    }
    base.update(fields)
    return PlanNode(**base)


class TestTheBody:
    def test_every_section_under_its_heading_then_the_marker(self) -> None:
        node = _task(
            goal="Plans survive a restart.",
            context="The daemon keeps its state in one store.",
            acceptance_criteria=("a plan reads back", "a stale write is refused"),
            kind="code",
            verify_commands=("make test", "make lint"),
            depends_on=("node_a", "node_b"),
            non_goals="No UI.",
            constraints="One migration.",
        )
        body = render_body(
            node,
            dependencies={
                "node_a": ("o/r", ForgeRef(12, "https://github.com/o/r/issues/12", "open")),
                "node_b": ("o/lib", ForgeRef(4, "https://github.com/o/lib/issues/4", "open")),
            },
        )
        assert body == (
            "## Goal\n\nPlans survive a restart.\n\n"
            "## Context\n\nThe daemon keeps its state in one store.\n\n"
            "## Acceptance criteria\n\n- [ ] a plan reads back\n- [ ] a stale write is refused\n\n"
            "## Kind\n\ncode\n\n"
            "## Verify commands\n\n```\nmake test\nmake lint\n```\n\n"
            "## Depends on\n\n- #12\n- o/lib#4\n\n"
            "## Non-goals\n\nNo UI.\n\n"
            "## Constraints\n\nOne migration.\n\n"
            "<!-- sbx-plan: plan_p/node_t -->\n"
        )

    def test_empty_sections_are_left_out(self) -> None:
        assert render_body(_task()) == "<!-- sbx-plan: plan_p/node_t -->\n"

    def test_a_workload_task_names_its_profile(self) -> None:
        body = render_body(_task(kind="workload", workload_profile="briefs"))
        assert "## Kind\n\nworkload (workload profile `briefs`)" in body

    def test_only_a_task_says_its_kind(self) -> None:
        assert "## Kind" not in render_body(_task(level="epic", kind="code"))

    def test_a_verify_command_with_backticks_cannot_close_the_block(self) -> None:
        body = render_body(_task(verify_commands=("echo ```",)))
        assert "````\necho ```\n````" in body

    def test_a_dependency_not_on_the_forge_is_named_by_id(self) -> None:
        assert "- `node_x`" in render_body(_task(depends_on=("node_x",)))


class TestTheMarker:
    def test_found_again_in_a_body_a_person_edited(self) -> None:
        body = "A person's words.\n\n<!--sbx-plan:plan_p/node_t-->\n\nMore words."
        assert markers(body) == [("plan_p", "node_t")]
        assert marked(body, "plan_p", "node_t")
        assert not marked(body, "plan_p", "node_u")
        assert not marked(body, "plan_q", "node_t")

    def test_round_trips(self) -> None:
        assert markers(marker("plan_p", "node_t")) == [("plan_p", "node_t")]


class TestReferences:
    def test_same_repository_is_short_across_is_qualified(self) -> None:
        assert issue_reference("o/r", "O/R", 3) == "#3"
        assert issue_reference("group/sub/project", "group/other", 3) == "group/other#3"
