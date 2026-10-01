"""The scenarios every backend must pass.

Each drives the role protocols only and asserts on the shared types only:
nothing here names a path, a payload key or a forge. What a scenario
relies on beyond the operations themselves it declares with
``@pytest.mark.needs``; see ``tests/conformance/__init__.py``.
"""

from __future__ import annotations

import base64

import pytest

from lantern.vcs.model import (
    BaseRequirements,
    ChecksVerdict,
    FailedCheck,
    IssueRef,
    MergeOutcome,
    PostedFinding,
    PrRef,
    ReviewComment,
    ReviewThread,
    SubmittedReview,
)
from lantern.vcs.protocol import CAPABILITIES, ROLES, Capability, VcsOps
from tests.conformance.conftest import Subject


class TestTheBackendItself:
    def test_answers_every_role(self, subject: Subject) -> None:
        for role in ROLES:
            assert isinstance(subject.ops, role), f"{subject.kind} lacks {role.__name__}"
        assert isinstance(subject.ops, VcsOps)

    def test_reports_every_capability_in_three_states(self, subject: Subject) -> None:
        report = subject.ops.capabilities()
        assert set(report) == set(CAPABILITIES)
        assert all(isinstance(state, Capability) for state in report.values())


class TestRepository:
    def test_lookup_and_default_branch(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        assert ops.repo_lookup(repo) is not None
        assert ops.default_branch(repo) == subject.base

    def test_a_base_ref_resolves_and_an_unknown_branch_does_not(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        sha = ops.ref_lookup(repo, f"heads/{subject.base}")
        assert isinstance(sha, str) and sha
        assert ops.ref_lookup(repo, "heads/lantern/never-delivered") is None


class TestIssues:
    def test_lifecycle_with_labels(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        ref = ops.issue_create(repo, "the checks never run", "body", labels=["lantern:run"])
        assert isinstance(ref, IssueRef) and ref.number > 0 and ref.url
        issue = ops.issue_get(repo, ref.number)
        assert issue["state"] == "open"
        ops.issue_labels_add(repo, ref.number, ["lantern:in-progress"])
        ops.issue_label_remove(repo, ref.number, "lantern:run")
        ops.issue_label_remove(repo, ref.number, "never-there")  # absent is not an error
        url = ops.issue_comment(repo, ref.number, "claimed")
        assert isinstance(url, str) and url
        ops.issue_close(repo, ref.number, reason="completed")
        assert ops.issue_get(repo, ref.number)["state"] == "closed"

    def test_listing_by_label_finds_a_seeded_issue(self, subject: Subject) -> None:
        number = subject.seeds.existing_issue("queued work", ["lantern:run"])
        listed = subject.ops.issues_list(subject.repo, labels=["lantern:run"])
        assert any(entry.get("number") == number for entry in listed)
        assert all("pull_request" not in entry for entry in listed if isinstance(entry, dict))

    def test_update_a_title_and_a_body(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        ref = ops.issue_create(repo, "a draft title", "the first body")
        updated = ops.issue_update(repo, ref.number, title="the settled title", body="a new body")
        assert updated["title"] == "the settled title" and updated["body"] == "a new body"
        issue = ops.issue_get(repo, ref.number)
        assert issue["title"] == "the settled title" and issue["body"] == "a new body"
        ops.issue_update(repo, ref.number, body="only the body")
        issue = ops.issue_get(repo, ref.number)
        assert issue["title"] == "the settled title" and issue["body"] == "only the body"

    def test_a_managed_checklist_rewrites_only_its_block(self, subject: Subject) -> None:
        from lantern.vcs.checklist import (
            ChecklistEntry,
            add_child,
            parse_checklist,
            set_child_closed,
            update_checklist,
        )

        ops, repo = subject.ops, subject.repo
        person = "A person's own words.\n\n- [ ] a person's own item"
        parent = ops.issue_create(repo, "an epic", person)
        child = ChecklistEntry(f"{repo}#{parent.number + 1000}", "a task")
        assert update_checklist(ops, repo, parent.number, lambda b: add_child(b, child))
        assert update_checklist(
            ops, repo, parent.number, lambda b: set_child_closed(b, child.ref, closed=True)
        )
        body = str(ops.issue_get(repo, parent.number)["body"])
        assert body.startswith(person)
        assert parse_checklist(body) == [ChecklistEntry(child.ref, child.title, closed=True)]


class TestSubIssues:
    @pytest.mark.needs("sub_issues")
    def test_add_list_and_remove_a_child(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        parent = ops.issue_create(repo, "an epic")
        child = ops.issue_create(repo, "a task")
        assert ops.sub_issues_list(repo, parent.number) == []
        ops.sub_issue_add(repo, parent.number, child_repo=repo, child_number=child.number)
        (listed,) = ops.sub_issues_list(repo, parent.number)
        assert listed["number"] == child.number and listed["html_url"] == child.url
        ops.sub_issue_remove(repo, parent.number, child_repo=repo, child_number=child.number)
        assert ops.sub_issues_list(repo, parent.number) == []

    @pytest.mark.needs("sub_issues")
    def test_a_child_in_another_repository(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        other = f"{repo}-elsewhere"
        parent = ops.issue_create(repo, "an initiative")
        child = ops.issue_create(other, "an epic in another repository")
        ops.sub_issue_add(repo, parent.number, child_repo=other, child_number=child.number)
        (listed,) = ops.sub_issues_list(repo, parent.number)
        assert listed["html_url"] == child.url


class TestChange:
    @pytest.mark.needs("draft_changes")
    def test_create_as_draft_then_ready_then_merge(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        ref = ops.pr_create(repo, subject.base, "lantern/r1", "lantern: ship it", draft=True)
        assert isinstance(ref, PrRef) and ref.number > 0
        change = ops.pr_get(repo, ref.number)
        assert change["draft"] is True
        assert ops.pr_ready_for_review(str(change["node_id"])) is True
        assert ops.pr_get(repo, ref.number)["draft"] is False
        outcome = ops.pr_merge(repo, ref.number, method="squash", sha=str(change["head"]["sha"]))
        assert isinstance(outcome, MergeOutcome) and outcome.merged and outcome.sha
        assert ops.pr_get(repo, ref.number)["merged"] is True

    def test_the_open_change_for_a_branch(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        assert ops.pr_list_open(repo, head="lantern/r1") == []
        ref = ops.pr_create(repo, subject.base, "lantern/r1", "lantern: ship it")
        (found,) = ops.pr_list_open(repo, head="lantern/r1")
        assert found["number"] == ref.number


class TestReview:
    @pytest.mark.needs("review_threads")
    def test_submit_reply_and_resolve(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        ref = ops.pr_create(repo, subject.base, "lantern/r1", "lantern: ship it")
        finding = ReviewComment(path="a.py", line=3, body="[major] this leaks")
        review = ops.pr_review_create(repo, ref.number, "REQUEST_CHANGES", "one leak", [finding])
        # GitLab discovers this licensed feature on the MR during submission.
        # Unknown or unread support still fails the same capability gate.
        from tests.conformance.gate import check_needs

        check_needs(subject.kind, ops.capabilities(), ["request_changes_review"])
        assert isinstance(review, SubmittedReview) and review.gates_merge
        (posted,) = review.posted
        assert isinstance(posted, PostedFinding)
        assert posted.anchor == "a.py:3" and posted.comment_id and posted.thread_id
        threads = ops.pr_review_threads(repo, ref.number)
        (thread,) = [t for t in threads if t.thread_id == posted.thread_id]
        assert isinstance(thread, ReviewThread) and not thread.is_resolved
        assert thread.anchor == "a.py:3"
        ops.pr_comment_reply(repo, ref.number, posted.comment_id, "fixed in the next push")
        (thread,) = [
            t for t in ops.pr_review_threads(repo, ref.number) if t.thread_id == posted.thread_id
        ]
        assert len(thread.comments) == 2
        assert ops.resolve_review_thread(posted.thread_id) is True
        (thread,) = [
            t for t in ops.pr_review_threads(repo, ref.number) if t.thread_id == posted.thread_id
        ]
        assert thread.is_resolved

    def test_a_change_level_comment(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        ref = ops.pr_create(repo, subject.base, "lantern/r1", "lantern: ship it")
        url = ops.pr_issue_comment(repo, ref.number, "the review could not be posted")
        assert isinstance(url, str) and url


class TestChecks:
    def test_a_green_head_and_a_red_head_with_its_log(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        ref = ops.pr_create(repo, subject.base, "lantern/r1", "lantern: ship it")
        head = str(ops.pr_get(repo, ref.number)["head"]["sha"])
        verdict = ops.pr_checks(repo, head)
        assert isinstance(verdict, ChecksVerdict) and verdict.state == "green"
        subject.seeds.failed_check("ci", "AssertionError: expected 2, got 3")
        verdict = ops.pr_checks(repo, head)
        assert verdict.state == "red" and verdict.failed == ("ci",)
        (failed,) = ops.checks_failed_logs(repo, head)
        assert isinstance(failed, FailedCheck)
        assert failed.name == "ci" and "expected 2, got 3" in failed.excerpt

    def test_a_pending_head_is_not_green(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        ref = ops.pr_create(repo, subject.base, "lantern/r1", "lantern: ship it")
        head = str(ops.pr_get(repo, ref.number)["head"]["sha"])
        subject.seeds.pending_check("ci")
        verdict = ops.pr_checks(repo, head)
        assert verdict.state == "pending" and verdict.pending == ("ci",)


class TestPolicy:
    @pytest.mark.needs("required_checks_introspection")
    def test_base_requirements_from_the_bases_rules(self, subject: Subject) -> None:
        seeded = subject.seeds.base_rules(required=["ci", "lint"], approvals=1)
        requirements = subject.ops.base_requirements(subject.repo, subject.base)
        assert isinstance(requirements, BaseRequirements)
        assert requirements.required_contexts is not None
        assert set(requirements.required_contexts) == set(seeded.required)
        assert requirements.all_checks_required is seeded.all_checks_required
        assert requirements.approvals_required == seeded.approvals
        assert requirements.source != "unknown"
        assert requirements.forge == subject.kind

    def test_a_base_with_no_named_check_is_an_answer(self, subject: Subject) -> None:
        requirements = subject.ops.base_requirements(subject.repo, subject.base)
        assert requirements.required_contexts == ()
        assert requirements.source != "unknown"
        assert requirements.blockers() == []


class TestContent:
    @pytest.mark.needs("remote_commit")
    def test_a_commit_without_a_checkout(self, subject: Subject) -> None:
        ops, repo = subject.ops, subject.repo
        base_sha = ops.ref_lookup(repo, f"heads/{subject.base}")
        assert base_sha
        base_tree = str(ops.commit_get(repo, base_sha)["tree"]["sha"])
        content = base64.b64encode(b"hello\n").decode()
        shas = ops.blobs_create_many(repo, [{"path": "hello.txt", "content_b64": content}])
        assert set(shas) == {"hello.txt"}
        tree = ops.tree_create(
            repo,
            base_tree=base_tree,
            entries=[
                {"path": "hello.txt", "mode": "100644", "type": "blob", "sha": shas["hello.txt"]}
            ],
        )
        commit = ops.commit_create(
            repo, message="lantern run r1: deliver", tree=str(tree["sha"]), parents=[base_sha]
        )
        ops.ref_create(repo, "refs/heads/lantern/r2", str(commit["sha"]))
        assert ops.ref_lookup(repo, "heads/lantern/r2") == commit["sha"]
        ops.ref_force_update(repo, "lantern/r2", str(commit["sha"]))
        assert ops.ref_lookup(repo, "heads/lantern/r2") == commit["sha"]
        # A fix round: the same branch carries a second commit of the base
        # plus a changed file, however the forge moves a branch.
        again = base64.b64encode(b"hello again\n").decode()
        shas = ops.blobs_create_many(repo, [{"path": "hello.txt", "content_b64": again}])
        tree = ops.tree_create(
            repo,
            base_tree=base_tree,
            entries=[
                {"path": "hello.txt", "mode": "100644", "type": "blob", "sha": shas["hello.txt"]}
            ],
        )
        second = ops.commit_create(
            repo, message="lantern run r1: deliver again", tree=str(tree["sha"]), parents=[base_sha]
        )
        ops.ref_force_update(repo, "lantern/r2", str(second["sha"]))
        head = ops.ref_lookup(repo, "heads/lantern/r2")
        assert head and head != commit["sha"]
        assert ops.commit_get(repo, head)["tree"]["sha"]

    @pytest.mark.needs("remote_commit")
    def test_a_file_on_a_branch_through_the_contents_path(self, subject: Subject) -> None:
        written = subject.ops.contents_put(
            subject.repo,
            "README.md",
            message="initialize",
            content_b64=base64.b64encode(b"# r\n").decode(),
            branch="lantern/r1",
        )
        assert written
        replaced = subject.ops.contents_put(
            subject.repo,
            "README.md",
            message="initialize again",
            content_b64=base64.b64encode(b"# r again\n").decode(),
            branch="lantern/r1",
        )
        assert replaced
