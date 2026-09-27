"""Writing an issue after it exists, and linking parent to child (#2338).

A plan publishes each node as an issue and edits it in place later, so
both backends answer ``issue_update``. GitHub links a parent to its
children natively (sub-issues, addressed by the child's issue *id*, not
its number); GitLab answers the ``sub_issues`` capability UNSUPPORTED — a
policy, so a plan on GitLab keeps its children in a managed checklist —
and its sub-issue operations refuse by name instead of guessing."""

from __future__ import annotations

import pytest

from sbxloop.errors import CapabilityUnsupported, GithubOpsError
from sbxloop.vcs.github.ops import GithubOps
from sbxloop.vcs.gitlab.ops import GitlabOps
from sbxloop.vcs.protocol import Capability
from tests.fakes.fake_github import FakeGithub
from tests.fakes.fake_gitlab import FakeGitlab


class TestTheCapability:
    def test_github_links_children_natively(self) -> None:
        assert GithubOps.CAPABILITIES["sub_issues"] is Capability.SUPPORTED

    def test_gitlab_never_does_whatever_its_tier(self) -> None:
        assert GitlabOps.CAPABILITIES["sub_issues"] is Capability.UNSUPPORTED
        assert FakeGitlab().capabilities()["sub_issues"] is Capability.UNSUPPORTED


class TestGithubIssueUpdate:
    def test_only_the_fields_given_are_sent(self) -> None:
        fake = FakeGithub()
        ref = fake.issue_create("o/r", "title", "body")
        fake.issue_update("o/r", ref.number, body="a new body")
        method, path, body = fake.raw_calls[-1]
        assert (method, path, body) == (
            "PATCH",
            f"/repos/o/r/issues/{ref.number}",
            {"body": "a new body"},
        )

    def test_nothing_to_write_is_refused_before_the_forge_is_asked(self) -> None:
        fake = FakeGithub()
        with pytest.raises(ValueError, match="title or a body"):
            fake.issue_update("o/r", 1)
        assert fake.raw_calls == []


class TestGithubSubIssues:
    def test_the_child_is_named_by_its_id_not_its_number(self) -> None:
        fake = FakeGithub()
        parent = fake.issue_create("o/r", "epic")
        child = fake.issue_create("o/r", "task")
        child_id = fake.issue_get("o/r", child.number)["id"]
        assert child_id != child.number
        fake.sub_issue_add("o/r", parent.number, child_repo="o/r", child_number=child.number)
        post = [c for c in fake.raw_calls if c[0] == "POST" and c[1].endswith("/sub_issues")]
        assert post == [
            ("POST", f"/repos/o/r/issues/{parent.number}/sub_issues", {"sub_issue_id": child_id})
        ]

    def test_a_child_with_a_parent_already_is_githubs_refusal(self) -> None:
        fake = FakeGithub()
        first = fake.issue_create("o/r", "one epic")
        second = fake.issue_create("o/r", "another epic")
        child = fake.issue_create("o/r", "task")
        fake.sub_issue_add("o/r", first.number, child_repo="o/r", child_number=child.number)
        with pytest.raises(GithubOpsError) as raised:
            fake.sub_issue_add("o/r", second.number, child_repo="o/r", child_number=child.number)
        assert raised.value.http_status == 422
        (listed,) = fake.sub_issues_list("o/r", first.number)
        assert listed["number"] == child.number

    def test_removing_a_child_sends_its_id(self) -> None:
        fake = FakeGithub()
        parent = fake.issue_create("o/r", "epic")
        child = fake.issue_create("o/r", "task")
        fake.sub_issue_add("o/r", parent.number, child_repo="o/r", child_number=child.number)
        fake.sub_issue_remove("o/r", parent.number, child_repo="o/r", child_number=child.number)
        child_id = fake.issue_get("o/r", child.number)["id"]
        assert (
            "DELETE",
            f"/repos/o/r/issues/{parent.number}/sub_issue",
            {"sub_issue_id": child_id},
        ) in fake.raw_calls
        assert fake.sub_issues_list("o/r", parent.number) == []


class TestGitlab:
    def test_update_writes_the_description(self) -> None:
        fake = FakeGitlab()
        ref = fake.issue_create("acme/widgets", "title", "body")
        record = fake.issue_update("acme/widgets", ref.number, title="new", body="new body")
        assert record["title"] == "new" and record["body"] == "new body"
        method, path, body = fake.raw_calls[-1]
        assert method == "PUT" and path.endswith(f"/issues/{ref.number}")
        assert body == {"title": "new", "description": "new body"}

    @pytest.mark.parametrize("operation", ["sub_issue_add", "sub_issue_remove"])
    def test_linking_refuses_by_name_and_asks_nothing(self, operation: str) -> None:
        fake = FakeGitlab()
        before = len(fake.raw_calls)
        with pytest.raises(CapabilityUnsupported) as raised:
            getattr(fake, operation)("acme/widgets", 1, child_repo="acme/widgets", child_number=2)
        assert raised.value.capability == "sub_issues"
        assert "checklist" in str(raised.value)
        assert len(fake.raw_calls) == before

    def test_listing_refuses_by_name(self) -> None:
        with pytest.raises(CapabilityUnsupported):
            FakeGitlab().sub_issues_list("acme/widgets", 1)
