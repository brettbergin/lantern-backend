"""Reconciling a published plan from the forge: the forge wins (#2342).

After publish a person edits, closes, adds and removes issues on the forge.
Reconciling reads the tree back and folds it in — never writing to the
forge — and reports each change as drift until someone marks it seen:
edits update the node, a child added on the forge is adopted with
``origin = forge``, a child removed or deleted is detached (never
recreated), and a broken marker or managed checklist is reported, not
repaired.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import select

from sbxloop.config import Config
from sbxloop.db.api_models import ApiEventRow
from sbxloop.errors import GithubOpsError
from sbxloop.plans.model import Drift, Plan, PlanNode
from sbxloop.plans.reconcile import Reconciliation, merge_drift, reconcile_plan
from sbxloop.plans.render import marker
from sbxloop.plans.store import PlanStore
from sbxloop.vcs.checklist import END, START, ChecklistEntry, add_child, remove_child
from tests.fakes.fake_github import FakeGithub
from tests.fakes.fake_gitlab import FakeGitlab
from tests.unit.test_plans_publish import (
    NOW,
    _epic,
    _github,
    _gitlab,
    _node,
    _publish,
    _save,
    _store,
)

LATER = NOW + 600


def _reconcile(ops: Any, store: PlanStore, config: Config, at: float = LATER) -> Reconciliation:
    plan = store.get("plan_1")
    assert plan is not None
    return reconcile_plan(ops, store=store, config=config, plan=plan, clock=lambda: at)


def _published(ops: Any, store: PlanStore, config: Config, nodes: list[PlanNode]) -> Plan:
    _save(store, nodes)
    result = _publish(ops, store, config, nodes[0].id)
    assert not result.failed, result.results
    plan = store.get("plan_1")
    assert plan is not None
    return plan


def _number(plan: Plan, node_id: str) -> int:
    node = plan.node(node_id)
    assert node is not None and node.forge is not None
    return node.forge.number


def _drift(store: PlanStore) -> list[dict[str, Any]]:
    with store.dstore.read() as session:
        rows = session.scalars(
            select(ApiEventRow).where(ApiEventRow.type == "plan.drift").order_by(ApiEventRow.seq)
        )
        return [json.loads(row.data_json) for row in rows]


def _writes(fake: FakeGithub, since: int) -> list[tuple[str, str]]:
    """Every write sbxloop sent the forge after call ``since``."""
    return [(m, p) for m, p, _ in fake.raw_calls[since:] if m != "GET"]


class TestNothingChanged:
    def test_a_fresh_plan_reads_back_as_it_was_published(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        nodes = _epic()
        nodes[2] = replace(nodes[2], non_goals="No UI.", context="A store.\n\n## Detail\n\nIt.")
        plan = _published(fake, store, config, nodes)
        calls, created = len(fake.raw_calls), len(fake.issues_created)
        result = _reconcile(fake, store, config)
        assert result.changes == 0 and result.error is None
        assert result.plan.revision == plan.revision
        assert result.plan.nodes == plan.nodes
        assert result.plan.reconciled_at == LATER
        assert _drift(store) == []
        assert _writes(fake, calls) == [] and len(fake.issues_created) == created

    def test_a_plan_with_nothing_published_reads_nothing(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        _save(store, _epic())
        result = _reconcile(fake, store, config)
        assert result.changes == 0 and fake.raw_calls == []


class TestEditsOnTheForge:
    def test_a_title_and_section_edit_is_folded_in_and_never_written_back(
        self, tmp_path: Path
    ) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        a = _number(plan, "a")
        body = str(fake.issue_get("o/r", a)["body"])
        edited = (
            "Someone's preface.\n\n"
            + body.replace("- [ ] stored", "- [x] stored\n- [ ] audited")
            + "\n## Team notes\n\nnot ours\n"
        )
        fake.person_edits("o/r", a, title="A, renamed", body=edited)
        calls = len(fake.raw_calls)
        result = _reconcile(fake, store, config)
        node = result.plan.node("a")
        assert node is not None
        assert node.title == "A, renamed"
        assert node.acceptance_criteria == ("stored", "audited")
        assert [(d.change, d.before, d.after) for d in node.drift] == [
            ("title", {"title": "a"}, {"title": "A, renamed"}),
            (
                "sections",
                {"acceptance_criteria": ["stored"]},
                {"acceptance_criteria": ["stored", "audited"]},
            ),
        ]
        assert result.plan.revision == plan.revision + 1
        assert [(e["node_id"], e["change"]) for e in _drift(store)] == [
            ("a", "title"),
            ("a", "sections"),
        ]
        assert _drift(store)[1]["fields"] == ["acceptance_criteria"]
        # Nothing was written to the forge: the person's edit stands.
        assert _writes(fake, calls) == [] and fake.issues_updated == []
        assert fake.issue_get("o/r", a)["body"] == edited
        # Read again, the same edit is not reported twice.
        again = _reconcile(fake, store, config, LATER + 60)
        assert again.changes == 0 and len(_drift(store)) == 2

    def test_a_second_edit_keeps_what_a_person_last_saw_and_a_revert_clears_it(
        self, tmp_path: Path
    ) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        epic = _number(plan, "epic")
        fake.person_edits("o/r", epic, title="Second")
        _reconcile(fake, store, config)
        fake.person_edits("o/r", epic, title="Third")
        node = _reconcile(fake, store, config, LATER + 60).plan.node("epic")
        assert node is not None
        assert [(d.before, d.after) for d in node.drift] == [
            ({"title": "epic"}, {"title": "Third"})
        ]
        fake.person_edits("o/r", epic, title="epic")
        node = _reconcile(fake, store, config, LATER + 120).plan.node("epic")
        assert node is not None and node.title == "epic" and node.drift == ()

    def test_a_dependency_rewritten_on_the_forge_names_the_sibling(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        a, b = _number(plan, "a"), _number(plan, "b")
        body = str(fake.issue_get("o/r", b)["body"])
        fake.person_edits("o/r", b, body=body.replace(f"- #{a}\n", ""))
        node = _reconcile(fake, store, config).plan.node("b")
        assert node is not None and node.depends_on == ()
        fake.person_edits("o/r", b, body=body)
        node = _reconcile(fake, store, config, LATER + 60).plan.node("b")
        assert node is not None and node.depends_on == ("a",)

    def test_closing_an_issue_is_its_forge_state(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        fake.person_edits("o/r", _number(plan, "a"), state="closed")
        node = _reconcile(fake, store, config).plan.node("a")
        assert node is not None and node.forge is not None
        assert node.forge.state == "closed"
        assert [(d.change, d.before, d.after) for d in node.drift] == [
            ("state", {"state": "open"}, {"state": "closed"})
        ]
        assert _drift(store)[0]["after"] == "closed"

    def test_a_removed_marker_is_reported_once_and_not_repaired(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        a = _number(plan, "a")
        stripped = str(fake.issue_get("o/r", a)["body"]).replace(marker("plan_1", "a"), "")
        fake.person_edits("o/r", a, body=stripped)
        calls = len(fake.raw_calls)
        node = _reconcile(fake, store, config).plan.node("a")
        assert node is not None and node.forge is not None and node.forge.marker_missing
        assert [d.change for d in node.drift] == ["marker_removed"]
        _reconcile(fake, store, config, LATER + 60)
        assert [e["change"] for e in _drift(store)] == ["marker_removed"]
        assert _writes(fake, calls) == []
        assert fake.issue_get("o/r", a)["body"] == stripped


class TestChildren:
    def test_a_sub_issue_added_on_the_forge_is_adopted(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        epic = _number(plan, "epic")
        written = fake.person_files(
            "o/r", "Audit the store", "## Goal\n\nEvery write audited.\n\n## Kind\n\ncode\n"
        )
        free = fake.person_files("o/r", "Loose ends", "Tidy up after the release.")
        other = fake.person_files(
            "o/r", "Elsewhere", "From another plan.\n\n" + marker("plan_9", "n")
        )
        for number in (written, free, other):
            fake.person_links("o/r", epic, "o/r", number)
        created = len(fake.issues_created)
        result = _reconcile(fake, store, config)
        adopted = [n for n in result.plan.nodes if n.origin == "forge"]
        assert [(n.title, n.level, n.state, n.parent_id) for n in adopted] == [
            ("Audit the store", "task", "published", "epic"),
            ("Loose ends", "task", "published", "epic"),
            ("Elsewhere", "task", "published", "epic"),
        ]
        first, second, _ = adopted
        assert first.goal == "Every write audited." and first.kind == "code"
        assert second.goal == "Tidy up after the release."
        assert first.forge is not None and first.forge.number == written
        assert [d.change for d in first.drift] == ["adopted"]
        assert [e["change"] for e in _drift(store)] == ["adopted"] * 3
        assert _drift(store)[0]["parent_id"] == "epic"
        assert len(fake.issues_created) == created
        # Read again: adopted once, not twice.
        again = _reconcile(fake, store, config, LATER + 60)
        assert again.changes == 0
        assert sum(1 for n in again.plan.nodes if n.origin == "forge") == 3
        # An adopted issue's free text is its goal from then on.
        fake.person_edits("o/r", free, body="Tidy up, and write it down.")
        edited = _reconcile(fake, store, config, LATER + 120).plan.node(second.id)
        assert edited is not None and edited.goal == "Tidy up, and write it down."
        assert [d.change for d in edited.drift] == ["adopted", "sections"]

    def test_an_issue_of_ours_not_recorded_yet_is_left_to_publishing(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        # A publish of C that created and linked its issue, then died.
        number = fake.person_files("o/r", "c", "## Goal\n\nc\n\n" + marker("plan_1", "c"))
        fake.person_links("o/r", _number(plan, "epic"), "o/r", number)
        result = _reconcile(fake, store, config)
        assert result.changes == 0
        assert not [n for n in result.plan.nodes if n.origin == "forge"]

    def test_a_child_removed_from_its_parent_is_detached_not_recreated(
        self, tmp_path: Path
    ) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        epic, b = _number(plan, "epic"), _number(plan, "b")
        fake.person_unlinks("o/r", epic, "o/r", b)
        calls, created = len(fake.raw_calls), len(fake.issues_created)
        node = _reconcile(fake, store, config).plan.node("b")
        assert node is not None and node.forge is not None
        assert node.state == "published" and node.forge.detached
        assert f"removed from o/r#{epic}" in node.forge.detached
        assert [d.change for d in node.drift] == ["detached"]
        assert _writes(fake, calls) == [] and len(fake.issues_created) == created
        # A detached node is not followed any more, and not detached twice.
        fake.person_edits("o/r", b, title="elsewhere now")
        again = _reconcile(fake, store, config, LATER + 60)
        assert again.changes == 0
        assert [e["change"] for e in _drift(store)] == ["detached"]
        # Linked again by a person, it follows its issue again.
        fake.person_links("o/r", epic, "o/r", b)
        node = _reconcile(fake, store, config, LATER + 120).plan.node("b")
        assert node is not None and node.forge is not None and node.forge.detached is None
        assert node.title == "elsewhere now"
        assert [d.change for d in node.drift] == ["detached", "reattached", "title"]

    def test_a_deleted_issue_is_detached(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        fake.person_deletes("o/r", _number(plan, "a"))
        calls = len(fake.raw_calls)
        node = _reconcile(fake, store, config).plan.node("a")
        assert node is not None and node.forge is not None
        assert node.forge.detached and "deleted" in node.forge.detached
        assert _writes(fake, calls) == []
        assert not [c for c in fake.issues_created if c[0] == "a"][1:]

    def test_a_task_moved_to_another_epic_moves(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        _published(
            fake,
            store,
            config,
            [
                _node("plan_1", "init", level="initiative", state="draft"),
                _node("plan_1", "e1", parent_id="init", position=0),
                _node("plan_1", "e2", parent_id="init", position=1),
                _node("plan_1", "t", parent_id="e1", level="task", kind="code"),
            ],
        )
        assert not _publish(fake, store, config, "e1").failed
        plan = store.get("plan_1")
        assert plan is not None
        e1, e2, t = _number(plan, "e1"), _number(plan, "e2"), _number(plan, "t")
        fake.person_unlinks("o/r", e1, "o/r", t)
        fake.person_links("o/r", e2, "o/r", t)
        node = _reconcile(fake, store, config).plan.node("t")
        assert node is not None and node.parent_id == "e2"
        assert node.forge is not None and node.forge.detached is None
        assert [(d.change, d.before, d.after) for d in node.drift] == [
            ("moved", {"parent_id": "e1"}, {"parent_id": "e2"})
        ]


class TestAPlanThatChangesMeanwhile:
    def test_an_edit_in_sbxloop_during_the_read_is_folded_again(self, tmp_path: Path) -> None:
        config, fake = _github(tmp_path), FakeGithub()

        class Busy(PlanStore):
            raced = False

            def apply(self, plan_id: str, **kwargs: Any) -> Plan:
                if not self.raced and kwargs.get("reconciled") is not None:
                    self.raced = True
                    plan = self.get(plan_id)
                    assert plan is not None
                    c = plan.node("c")
                    assert c is not None
                    super().apply(
                        plan_id,
                        expected_revision=plan.revision,
                        now=LATER,
                        upsert=[replace(c, title="edited in sbxloop")],
                    )
                return super().apply(plan_id, **kwargs)

        store = Busy(_store(tmp_path).dstore)
        plan = _published(fake, store, config, _epic())
        fake.person_edits("o/r", _number(plan, "a"), title="edited on the forge")
        result = _reconcile(fake, store, config)
        titles = {n.id: n.title for n in result.plan.nodes}
        assert titles["a"] == "edited on the forge" and titles["c"] == "edited in sbxloop"
        assert result.plan.revision == plan.revision + 2
        assert [e["change"] for e in _drift(store)] == ["title"]


class TestTheForgeWouldNotAnswer:
    def test_an_issue_that_cannot_be_read_is_named_and_left_alone(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        fake.fail_always["issue_read"] = GithubOpsError("bad gateway", http_status=502)
        result = _reconcile(fake, store, config)
        assert result.changes == 0
        assert result.error and "bad gateway" in result.error
        assert result.plan.nodes == plan.nodes
        assert result.plan.reconcile_error == result.error

    def test_sub_issues_that_cannot_be_listed_detach_nothing(self, tmp_path: Path) -> None:
        config, store, fake = _github(tmp_path), _store(tmp_path), FakeGithub()
        plan = _published(fake, store, config, _epic())
        fake.person_edits("o/r", _number(plan, "a"), state="closed")
        fake.fail_always["sub_issues_list"] = GithubOpsError("rate limited", http_status=403)
        result = _reconcile(fake, store, config)
        assert result.error and "could not list the sub-issues" in result.error
        states = {n.id: (n.forge.state, n.forge.detached) for n in result.plan.nodes if n.forge}
        # Each child is still read on its own: its state folds in.
        assert states["a"] == ("closed", None) and states["b"] == ("open", None)


class TestGitlab:
    def test_edits_close_and_a_person_added_line_on_the_checklist(self, tmp_path: Path) -> None:
        config, store, fake = _gitlab(tmp_path), _store(tmp_path), FakeGitlab()
        plan = _published(fake, store, config, _epic("acme/widgets"))
        epic, a = _number(plan, "epic"), _number(plan, "a")
        fake.person_edits(a, title="A on GitLab", state="closed")
        extra = fake.person_files("Follow-up", "## Goal\n\nFollow it up.")
        description = str(fake.issues[epic]["description"])
        with_line = add_child(description, ChecklistEntry(f"acme/widgets#{extra}", "Follow-up"))
        fake.person_edits(epic, description=with_line)
        calls = len(fake.raw_calls)
        result = _reconcile(fake, store, config)
        node = result.plan.node("a")
        assert node is not None and node.forge is not None
        assert node.title == "A on GitLab" and node.forge.state == "closed"
        adopted = [n for n in result.plan.nodes if n.origin == "forge"]
        assert [(n.title, n.goal, n.parent_id) for n in adopted] == [
            ("Follow-up", "Follow it up.", "epic")
        ]
        assert [m for m, _, _ in fake.raw_calls[calls:] if m != "GET"] == []

    def test_a_mangled_checklist_is_reported_and_judges_no_child(self, tmp_path: Path) -> None:
        config, store, fake = _gitlab(tmp_path), _store(tmp_path), FakeGitlab()
        plan = _published(fake, store, config, _epic("acme/widgets"))
        epic = _number(plan, "epic")
        mangled = str(fake.issues[epic]["description"]).replace(END, "")
        fake.person_edits(epic, description=mangled)
        calls = len(fake.raw_calls)
        result = _reconcile(fake, store, config)
        root = result.plan.node("epic")
        assert root is not None and root.forge is not None
        assert root.forge.checklist_error and "closing marker" in root.forge.checklist_error
        assert [d.change for d in root.drift] == ["checklist_mangled"]
        assert all(n.forge is None or n.forge.detached is None for n in result.plan.nodes)
        assert fake.issues[epic]["description"] == mangled
        assert [m for m, _, _ in fake.raw_calls[calls:] if m != "GET"] == []

    def test_a_block_removed_whole_is_reported_not_read_as_no_children(
        self, tmp_path: Path
    ) -> None:
        config, store, fake = _gitlab(tmp_path), _store(tmp_path), FakeGitlab()
        plan = _published(fake, store, config, _epic("acme/widgets"))
        epic = _number(plan, "epic")
        description = str(fake.issues[epic]["description"])
        fake.person_edits(epic, description=description[: description.index(START)])
        result = _reconcile(fake, store, config)
        root = result.plan.node("epic")
        assert root is not None and root.forge is not None
        assert root.forge.checklist_error and "gone" in root.forge.checklist_error
        assert all(n.forge is None or n.forge.detached is None for n in result.plan.nodes)

    def test_a_line_removed_detaches_and_a_deleted_issue_detaches(self, tmp_path: Path) -> None:
        config, store, fake = _gitlab(tmp_path), _store(tmp_path), FakeGitlab()
        plan = _published(fake, store, config, _epic("acme/widgets"))
        epic, a, b = _number(plan, "epic"), _number(plan, "a"), _number(plan, "b")
        description = str(fake.issues[epic]["description"])
        fake.person_edits(epic, description=remove_child(description, f"acme/widgets#{a}"))
        fake.person_deletes(b)
        result = _reconcile(fake, store, config)
        detached = {n.id: n.forge.detached for n in result.plan.nodes if n.forge}
        assert detached["a"] and "removed from" in detached["a"]
        assert detached["b"] and "deleted" in detached["b"]
        assert detached["epic"] is None


class TestDriftBookkeeping:
    def test_merging_keeps_the_first_before_and_the_last_after(self) -> None:
        first = Drift("sections", 1.0, {"goal": "a"}, {"goal": "b"})
        second = Drift(
            "sections", 2.0, {"goal": "b", "context": "x"}, {"goal": "c", "context": "y"}
        )
        (merged,) = merge_drift((first,), second)
        assert merged.before == {"goal": "a", "context": "x"}
        assert merged.after == {"goal": "c", "context": "y"} and merged.at == 2.0
        back = Drift("sections", 3.0, {"goal": "c", "context": "y"}, {"goal": "a", "context": "x"})
        assert merge_drift((merged,), back) == ()
        detached = Drift("detached", 4.0, reason="gone")
        assert merge_drift((detached,), detached) == (detached, detached)
