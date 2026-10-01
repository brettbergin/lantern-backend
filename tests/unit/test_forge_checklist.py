"""The managed children checklist in a parent issue (#2339).

On a forge with no native sub-issues (GitLab, by policy), a published
parent lists its children in a block of its description between
``<!-- sbx-plan:children -->`` markers. lantern rewrites that block and
nothing else, and a block a person broke is reported, never repaired.
"""

from __future__ import annotations

import pytest

from lantern.vcs.checklist import (
    END,
    START,
    ChecklistEntry,
    ChecklistMangled,
    add_child,
    parse_checklist,
    remove_child,
    render_checklist,
    set_child_closed,
    update_checklist,
)
from tests.fakes.fake_gitlab import FakeGitlab

REPO = "acme/widgets"
A = ChecklistEntry("acme/widgets#11", "Store plans")
B = ChecklistEntry("acme/tools#4", "Serve the routes")

PERSON = "Why this epic exists, in a person's words.\n\n- [ ] a person's own list item\n"


def _body_with(entries: list[ChecklistEntry]) -> str:
    return PERSON + "\n" + render_checklist(entries) + "\n\nA footer a person wrote.\n"


class TestTheBlock:
    def test_renders_between_its_markers_one_line_per_child(self) -> None:
        block = render_checklist([A, ChecklistEntry(B.ref, B.title, closed=True)])
        assert block == (
            f"{START}\n"
            "- [ ] acme/widgets#11 Store plans\n"
            "- [x] acme/tools#4 Serve the routes\n"
            f"{END}"
        )

    def test_reads_back_what_it_wrote(self) -> None:
        body = _body_with([A, ChecklistEntry(B.ref, B.title, closed=True)])
        assert parse_checklist(body) == [A, ChecklistEntry(B.ref, B.title, closed=True)]

    def test_a_body_without_a_block_has_no_children(self) -> None:
        assert parse_checklist(PERSON) == []


class TestOnlyTheBlockChanges:
    def test_adding_a_child_to_a_body_without_a_block_appends_one(self) -> None:
        body = add_child(PERSON, A)
        assert body.startswith(PERSON)
        assert parse_checklist(body) == [A]

    def test_adding_removing_and_closing_leave_the_rest_alone(self) -> None:
        body = _body_with([A])
        before, _, after = body.partition(START)
        after = after.partition(END)[2]
        for step in (
            lambda b: add_child(b, B),
            lambda b: set_child_closed(b, A.ref, closed=True),
            lambda b: remove_child(b, B.ref),
        ):
            body = step(body)
            assert body.startswith(before) and body.endswith(after)
        assert parse_checklist(body) == [ChecklistEntry(A.ref, A.title, closed=True)]

    def test_adding_a_child_already_listed_changes_nothing(self) -> None:
        body = _body_with([A])
        assert add_child(body, ChecklistEntry(A.ref, "a new title")) == body

    def test_removing_an_absent_child_changes_nothing(self) -> None:
        body = _body_with([A])
        assert remove_child(body, "acme/widgets#99") == body


class TestAMangledBlockIsReported:
    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            (f"{PERSON}\n{START}\n- [ ] acme/widgets#11 Store plans\n", "no closing marker"),
            (f"{PERSON}\n- [ ] acme/widgets#11 Store plans\n{END}\n", "no opening marker"),
            (
                f"{START}\n- [ ] acme/widgets#11 x\n{END}\n{START}\n{END}\n",
                "more than one",
            ),
            (f"{START}\n- [ ] acme/widgets#11 x\nsome words\n{END}\n", "line 2"),
        ],
    )
    def test_named_and_never_repaired(self, body: str, reason: str) -> None:
        with pytest.raises(ChecklistMangled, match=reason):
            parse_checklist(body)
        with pytest.raises(ChecklistMangled):
            add_child(body, B)


class TestOnTheForge:
    def test_the_parents_description_is_rewritten_only_when_it_changes(self) -> None:
        fake = FakeGitlab(repo=REPO)
        parent = fake.issue_create(REPO, "An epic", PERSON)
        assert update_checklist(fake, REPO, parent.number, lambda b: add_child(b, A)) is True
        description = fake.issue_get(REPO, parent.number)["body"]
        assert description.startswith(PERSON) and parse_checklist(description) == [A]
        writes = len([c for c in fake.raw_calls if c[0] == "PUT"])
        assert update_checklist(fake, REPO, parent.number, lambda b: add_child(b, A)) is False
        assert len([c for c in fake.raw_calls if c[0] == "PUT"]) == writes

    def test_a_mangled_block_on_the_forge_is_left_as_it_is(self) -> None:
        fake = FakeGitlab(repo=REPO)
        broken = f"{PERSON}\n{START}\n- [ ] acme/widgets#11 x\n"
        parent = fake.issue_create(REPO, "An epic", broken)
        with pytest.raises(ChecklistMangled):
            update_checklist(fake, REPO, parent.number, lambda b: add_child(b, B))
        assert fake.issue_get(REPO, parent.number)["body"] == broken
