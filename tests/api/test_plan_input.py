"""The intake brief is inference input, never the root issue's content."""

from __future__ import annotations

import pytest

from sbxloop.engine.planning import PlanProposal, proposal_problems
from sbxloop.plans.service import PlanRefusal
from tests.api.conftest import Api
from tests.api.test_plans import DRAFT, _create


@pytest.mark.parametrize("level", ["initiative", "epic"])
def test_input_is_separate_and_the_planner_generates_the_root(api: Api, level: str) -> None:
    plan = _create(api, api.bearer(DRAFT), level=level, title="my rough ask", goal="user goal")
    root = plan["nodes"][0]
    assert plan["input"]["title"] == "my rough ask"
    assert plan["generation_pending"] is True
    assert root["title"] != "my rough ask" and root["goal"] == ""
    brief = api.ctx.plans.brief(plan["id"], plan["root_id"])
    assert brief.generate_root and brief.input["goal"] == "user goal"
    sections = {
        "title": "Generated outcome",
        "goal": "Generated goal",
        "context": "Read the repo",
        "acceptance_criteria": ["Observable outcome"],
    }
    child = dict(sections, title="Generated child")
    if level == "epic":
        child.update(kind="code", verify_commands=["make test"])
    proposal = PlanProposal.model_validate({"root": sections, "children": [child]})
    assert proposal_problems(proposal, brief) == []
    changed, count = api.ctx.plans.deliver_proposal(
        plan["id"], root["id"], proposal, run_id="run1", now=2.0
    )
    assert count == 1 and changed.revision == plan["revision"] + 1
    assert changed.root.title == "Generated outcome" and changed.root.origin == "planner"
    assert changed.root.state == "proposed" and not changed.generation_pending
    assert changed.input["title"] == "my rough ask"
    assert changed.children(root["id"])[0].origin == "planner"


def test_missing_root_cannot_be_delivered_or_published(api: Api) -> None:
    plan = _create(api, api.bearer(DRAFT))
    proposal = PlanProposal.model_validate({"children": [{"title": "Child"}]})
    brief = api.ctx.plans.brief(plan["id"], plan["root_id"])
    assert any("root" in problem for problem in proposal_problems(proposal, brief))
    with pytest.raises(PlanRefusal, match="root"):
        api.ctx.plans.deliver_proposal(
            plan["id"], plan["root_id"], proposal, run_id="run1", now=2.0
        )
    with pytest.raises(PlanRefusal, match="Generate"):
        api.ctx.plans.publish(
            plan["id"],
            plan["root_id"],
            expected_revision=plan["revision"],
            forge_kind=None,
            connect=lambda: None,
            clock=lambda: 2.0,
            actor={},
        )
    assert api.ctx.plans.get(plan["id"]).revision == plan["revision"]


def test_editing_pending_root_updates_only_the_brief(api: Api) -> None:
    headers = api.bearer(DRAFT)
    plan = _create(api, headers)
    response = api.client.patch(
        f"/v1/plans/{plan['id']}",
        headers=headers,
        json={"expected_revision": plan["revision"], "goal": "Revised input"},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["input"]["goal"] == "Revised input"
    assert updated["nodes"][0]["goal"] == ""


def test_an_old_inference_result_cannot_overwrite_a_new_brief(api: Api) -> None:
    plan = _create(api, api.bearer(DRAFT))
    old = api.ctx.plans.get(plan["id"])
    proposal = PlanProposal.model_validate(
        {
            "source_input": old.input,
            "root": {
                "title": "Generated",
                "goal": "Outcome",
                "context": "Repository findings",
                "acceptance_criteria": ["Verified"],
            },
            "children": [{"title": "A child"}],
        }
    )
    changed = api.ctx.plans.update(
        plan["id"],
        expected_revision=old.revision,
        sections={"goal": "Changed my mind"},
        now=2.0,
        actor={},
    )
    with pytest.raises(PlanRefusal, match="brief changed"):
        api.ctx.plans.deliver_proposal(plan["id"], old.root_id, proposal, run_id="run1", now=3.0)
    assert api.ctx.plans.get(plan["id"]) == changed


def test_a_legacy_full_level_can_generate_its_parent_without_more_children(api: Api) -> None:
    from sbxloop.config import PlanningConfig

    api.loop.config.planning = PlanningConfig(max_epics_per_initiative=1)
    plan = _create(api, api.bearer(DRAFT))
    old = api.ctx.plans.get(plan["id"])
    old, child_id = api.ctx.plans.add_node(
        old.id,
        expected_revision=old.revision,
        parent_id=old.root_id,
        repository=None,
        sections={"title": "Keep this epic"},
        position=None,
        now=2.0,
        actor={},
    )
    brief = api.ctx.plans.brief(old.id, old.root_id)
    assert brief.generate_root and brief.room == 0
    proposal = PlanProposal.model_validate(
        {
            "root": {
                "title": "Generated initiative",
                "goal": "Outcome",
                "context": "Repository findings",
                "acceptance_criteria": ["Verified"],
            },
            "children": [],
        }
    )
    assert proposal_problems(proposal, brief) == []
    changed, count = api.ctx.plans.deliver_proposal(
        old.id, old.root_id, proposal, run_id="run1", now=3.0
    )
    assert count == 0 and changed.root.origin == "planner"
    assert changed.node(child_id) == old.node(child_id)
