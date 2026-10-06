"""A person's direct writes to a published plan over ``/v1/plans`` (#2350).

``PATCH .../nodes/{id}`` on a published node writes its title and sections
to the issue at once (``plans:publish``), naming the version of the issue
the client read: an issue that changed on the forge since is refused with
its current version and nothing is written. ``POST .../attach`` links an
existing open issue as a child, ``POST .../detach`` unlinks one without
closing it — a sub-issue on GitHub, a checklist line on GitLab. None of
Lantern's own writes shows up as drift on the next reconcile.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lantern.plans.render import marked, parse_sections
from lantern.vcs.checklist import parse_checklist
from tests.api.conftest import Api, build
from tests.api.test_plans_publish import (
    DRAFT,
    MANAGE,
    PUBLISH,
    _epic_with_tasks,
    _events,
    _forge,
    _node,
    _publish,
)
from tests.api.test_plans_reconcile import _number, _published, _sync
from tests.fakes.fake_gitlab import FakeGitlab


def _edit(
    api: Api,
    headers: dict[str, str],
    plan: dict[str, Any],
    node: dict[str, Any],
    version: str | None = None,
    **sections: Any,
) -> Any:
    body: dict[str, Any] = {"expected_revision": plan["revision"], **sections}
    if version != "":
        body["forge_version"] = version or node["forge"]["version"]
    return api.client.patch(
        f"/v1/plans/{plan['id']}/nodes/{node['id']}", json=body, headers=headers
    )


def _attach(
    api: Api, headers: dict[str, str], plan: dict[str, Any], parent_id: str, **body: Any
) -> Any:
    return api.client.post(
        f"/v1/plans/{plan['id']}/nodes/{parent_id}/attach",
        json={"expected_revision": plan["revision"], **body},
        headers=headers,
    )


def _detach(api: Api, headers: dict[str, str], plan: dict[str, Any], node_id: str) -> Any:
    return api.client.post(
        f"/v1/plans/{plan['id']}/nodes/{node_id}/detach",
        json={"expected_revision": plan["revision"]},
        headers=headers,
    )


def _changes(api: Api) -> list[str]:
    return [e["change"] for e in _events(api, "plan.node.changed")]


def _no_drift_after(api: Api, plan: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """A sync right after Lantern's own write: nothing drifted."""
    synced = _sync(api, plan, headers)
    assert synced.status_code == 200, synced.text
    body = dict(synced.json())
    assert body["drift"] == 0, [n["drift"] for n in body["nodes"]]
    assert body["revision"] == plan["revision"]
    assert _events(api, "plan.drift") == []
    return body


class TestEditingAPublishedNode:
    def test_the_issue_is_written_at_once_and_only_the_edited_sections(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        a = _node(plan, "A")
        number = _number(plan, "A")
        body = str(fake.issue_get("o/r", number)["body"])
        # A person ticked the criterion and wrote around our sections.
        ticked = "Their preface.\n\n" + body.replace("- [ ] works", "- [x] works")
        ticked += "\n## Team notes\n\nkeep me\n"
        fake.person_edits("o/r", number, body=ticked)
        plan = _sync(api, plan, headers).json()
        a = _node(plan, "A")
        edited = _edit(api, headers, plan, a, title="A, sharper", goal="Do A well.")
        assert edited.status_code == 200, edited.text
        node = _node(edited.json(), "A, sharper")
        assert node["goal"] == "Do A well." and node["state"] == "published"
        assert node["forge"]["version"] != a["forge"]["version"]
        issue = fake.issue_get("o/r", number)
        assert issue["title"] == "A, sharper"
        written = str(issue["body"])
        assert parse_sections(written)["goal"] == "Do A well."
        for kept in ("Their preface.", "- [x] works", "## Team notes\n\nkeep me"):
            assert kept in written
        assert marked(written, plan["id"], a["id"])
        (update,) = [u for n, u in fake.issues_updated if n == number]
        assert set(update) == {"title", "body"}
        (event,) = [e for e in _events(api, "plan.node.changed") if e["change"] == "issue_edited"]
        assert event["fields"] == ["title", "goal"] and event["number"] == number
        _no_drift_after(api, edited.json(), headers)

    def test_a_title_edit_leaves_the_body_alone(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        edited = _edit(api, headers, plan, _node(plan, "B"), title="B2")
        assert edited.status_code == 200, edited.text
        (update,) = fake.issues_updated
        assert update == (_number(plan, "B"), {"title": "B2"})
        _no_drift_after(api, edited.json(), headers)

    def test_a_stale_version_is_refused_with_the_forges_and_nothing_is_written(
        self, api: Api
    ) -> None:
        fake, plan, headers = _published(api)
        a = _node(plan, "A")
        number = _number(plan, "A")
        body = str(fake.issue_get("o/r", number)["body"])
        theirs = body.replace(
            "## Acceptance criteria", "## Context\n\nTheirs.\n\n## Acceptance criteria"
        )
        fake.person_edits("o/r", number, title="A, theirs", body=theirs)
        writes = len(fake.issues_updated)
        refused = _edit(api, headers, plan, a, title="A, mine", context="Mine.")
        assert refused.status_code == 409, refused.text
        problem = refused.json()
        assert problem["code"] == "forge_changed"
        current = problem["current"]
        assert current["title"] == "A, theirs" and current["context"] == "Theirs."
        assert current["forge_version"] == problem["forge_version"] != a["forge"]["version"]
        assert len(fake.issues_updated) == writes
        assert fake.issue_get("o/r", number)["title"] == "A, theirs"
        # Having seen it, the person edits again naming the forge's version:
        # what they did not touch keeps the forge's text.
        again = _edit(api, headers, plan, a, problem["forge_version"], title="A, mine")
        assert again.status_code == 200, again.text
        node = _node(again.json(), "A, mine")
        assert node["context"] == "Theirs."
        assert "Theirs." in str(fake.issue_get("o/r", number)["body"])
        _no_drift_after(api, again.json(), headers)

    def test_a_stale_revision_is_refused_before_the_forge(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        stale = {**plan, "revision": plan["revision"] - 1}
        calls = len(fake.raw_calls)
        refused = _edit(api, headers, stale, _node(plan, "A"), title="A2")
        assert refused.status_code == 409 and refused.json()["code"] == "stale_revision"
        assert fake.raw_calls[calls:] == []

    def test_the_version_read_is_required(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        refused = _edit(api, headers, plan, _node(plan, "A"), "", title="A2")
        assert refused.status_code == 422 and "forge_version" in refused.json()["detail"]
        assert fake.issues_updated == []

    def test_drafting_alone_cannot_edit_a_published_node_but_may_move_it(self, api: Api) -> None:
        fake, plan, _ = _published(api)
        drafter = api.bearer(DRAFT)
        refused = _edit(api, drafter, plan, _node(plan, "A"), title="A2")
        assert refused.status_code == 403, refused.text
        assert refused.json()["capability"] == "plans:publish"
        assert "plans:publish" in refused.json()["detail"]
        moved = api.client.patch(
            f"/v1/plans/{plan['id']}/nodes/{_node(plan, 'B')['id']}",
            json={"expected_revision": plan["revision"], "position": 0},
            headers=drafter,
        )
        assert moved.status_code == 200, moved.text
        assert fake.issues_updated == []

    def test_a_dependency_on_an_unpublished_sibling_is_refused(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        added = api.client.post(
            f"/v1/plans/{plan['id']}/nodes",
            json={
                "expected_revision": plan["revision"],
                "parent_id": plan["root_id"],
                "title": "C",
            },
            headers=headers,
        ).json()
        c = _node(added, "C")["id"]
        refused = _edit(api, headers, added, _node(added, "B"), depends_on=[c])
        assert refused.status_code == 409 and refused.json()["code"] == "dependency_unpublished"
        assert fake.issues_updated == []


class TestAttachingOnGithub:
    def test_an_open_issue_becomes_a_sub_issue_child_adopted_from_the_forge(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        number = fake.person_files("o/r", "Found work", "## Goal\n\nDo the found work.")
        epic = _number(plan, "An epic")
        attached = _attach(api, headers, plan, plan["root_id"], repository="o/r", number=number)
        assert attached.status_code == 200, attached.text
        answer = attached.json()
        assert answer["linked"] == "native" and answer["reason"] is None
        assert attached.headers["Location"].endswith(answer["node_id"])
        node = _node(answer["plan"], "Found work")
        assert node["id"] == answer["node_id"]
        assert (node["origin"], node["state"], node["level"]) == ("forge", "published", "task")
        assert node["goal"] == "Do the found work." and node["drift"] == []
        assert ("o/r", number) in fake.sub_issues[("o/r", epic)]
        labelled = [b for m, p, b in fake.raw_calls if p == f"/repos/o/r/issues/{number}/labels"]
        assert labelled == [{"labels": ["sbx:task"]}]
        assert "attached" in _changes(api)
        _no_drift_after(api, answer["plan"], headers)

    def test_an_issue_named_by_its_url(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        number = fake.person_files("o/r", "By URL")
        url = f"https://github.com/o/r/issues/{number}"
        attached = _attach(api, headers, plan, plan["root_id"], url=url)
        assert attached.status_code == 200, attached.text
        assert _node(attached.json()["plan"], "By URL")["forge"]["number"] == number

    def test_what_cannot_be_attached_is_refused_before_anything_is_linked(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        root = plan["root_id"]
        closed = fake.person_files("o/r", "Done already")
        fake.person_edits("o/r", closed, state="closed")
        pull = fake.person_files("o/r", "A change", pull_request=True)
        elsewhere = fake.person_files("o/r", "Someone's parent")
        owned = fake.person_files("o/r", "Owned")
        fake.person_links("o/r", elsewhere, "o/r", owned)
        cases = [
            ({"repository": "o/r", "number": closed}, 409, "issue_closed"),
            ({"repository": "o/r", "number": pull}, 422, "not_an_issue"),
            ({"repository": "o/r", "number": _number(plan, "A")}, 409, "already_in_plan"),
            ({"repository": "o/r", "number": owned}, 409, "already_has_parent"),
            ({"repository": "o/r", "number": 4040}, 404, "issue_not_found"),
            ({"url": "https://github.com/o/r/pull/3"}, 422, "invalid_argument"),
            ({"repository": "o/r"}, 422, "invalid_argument"),
        ]
        for body, status, code in cases:
            refused = _attach(api, headers, plan, root, **body)
            assert (refused.status_code, refused.json()["code"]) == (status, code), body
        linked = fake.sub_issues[("o/r", _number(plan, "An epic"))]
        assert not any(number in (closed, pull, owned) for _, number in linked)
        refused_paths = {f"/repos/o/r/issues/{n}/labels" for n in (closed, pull, owned)}
        assert not any(p in refused_paths for _, p, _ in fake.raw_calls)
        # A task has no children, and drafting alone attaches nothing.
        leaf = _attach(api, headers, plan, _node(plan, "A")["id"], repository="o/r", number=closed)
        assert leaf.status_code == 422
        drafter = _attach(api, api.bearer(DRAFT), plan, root, repository="o/r", number=closed)
        assert drafter.status_code == 403

    def test_a_task_lives_in_its_epics_repository(self, api: Api) -> None:
        api.client.post(
            "/v1/repositories", json={"repository": "o/other"}, headers=api.bearer(MANAGE)
        )
        fake, plan, headers = _published(api)
        number = fake.person_files("o/other", "Across")
        refused = _attach(api, headers, plan, plan["root_id"], repository="o/other", number=number)
        assert refused.status_code == 422 and "epic's repository" in refused.json()["detail"]

    def test_a_parent_at_its_cap_takes_no_more(self, tmp_path: Path) -> None:
        built = build(tmp_path, config={"planning": {"max_tasks_per_epic": 2}})
        with built.client:
            fake, plan, headers = _published(built)
            number = fake.person_files("o/r", "One too many")
            refused = _attach(
                built, headers, plan, plan["root_id"], repository="o/r", number=number
            )
            assert refused.status_code == 409 and refused.json()["code"] == "too_many_children"
        built.ctx.close()


class TestDetachingOnGithub:
    def test_the_link_goes_the_issue_stays_open_and_siblings_stop_depending(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        epic, a = _number(plan, "An epic"), _number(plan, "A")
        detached = _detach(api, headers, plan, _node(plan, "A")["id"])
        assert detached.status_code == 200, detached.text
        body = detached.json()
        node = _node(body, "A")
        assert node["forge"]["detached"] and "in the app" in node["forge"]["detached"]
        assert node["drift"] == []
        assert _node(body, "B")["depends_on"] == []
        # B's issue no longer says it depends on A; the rest of it stands.
        b_body = str(fake.issue_get("o/r", _number(plan, "B"))["body"])
        assert "Depends on" not in b_body and "## Kind" in b_body
        assert marked(b_body, plan["id"], _node(plan, "B")["id"])
        ((_, update),) = [u for u in fake.issues_updated if u[0] == _number(plan, "B")]
        assert set(update) == {"body"}
        assert ("o/r", a) not in fake.sub_issues[("o/r", epic)]
        assert fake.issue_get("o/r", a)["state"] == "open" and fake.issues_closed == []
        assert "detached" in _changes(api)
        _no_drift_after(api, body, headers)
        # Detached, it is not detached again, and editing it is refused.
        again = _detach(api, headers, body, node["id"])
        assert again.status_code == 409 and again.json()["code"] == "node_detached"
        # Attached again, it follows its issue once more.
        back = _attach(api, headers, body, plan["root_id"], repository="o/r", number=a)
        assert back.status_code == 200, back.text
        assert back.json()["node_id"] == node["id"]
        assert _node(back.json()["plan"], "A")["forge"]["detached"] is None
        _no_drift_after(api, back.json()["plan"], headers)

    def test_the_root_and_unpublished_nodes_are_not_detached(self, api: Api) -> None:
        _, plan, headers = _published(api)
        root = _detach(api, headers, plan, plan["root_id"])
        assert root.status_code == 422
        drafter = _detach(api, api.bearer(DRAFT), plan, _node(plan, "A")["id"])
        assert drafter.status_code == 403


def _gitlab(api: Api) -> tuple[FakeGitlab, dict[str, Any], dict[str, str]]:
    created = api.client.post(
        "/v1/repositories",
        json={"repository": "acme/widgets", "forge": "gitlab"},
        headers=api.bearer(MANAGE),
    )
    assert created.status_code == 201, created.text
    fake = _forge(api, FakeGitlab(), kind="gitlab")
    headers = api.bearer(PUBLISH)
    plan = _epic_with_tasks(api, headers, repo="acme/widgets")
    published = _publish(api, headers, plan)
    assert published.status_code == 200, published.text
    return fake, published.json()["plan"], headers


class TestOnGitlab:
    def test_attach_and_detach_are_checklist_lines_and_edits_write_the_description(
        self, api: Api
    ) -> None:
        fake, plan, headers = _gitlab(api)
        epic = _number(plan, "An epic")
        found = fake.person_files("Found on GitLab", "Loose words.")
        attached = _attach(
            api, headers, plan, plan["root_id"], repository="acme/widgets", number=found
        )
        assert attached.status_code == 200, attached.text
        assert attached.json()["linked"] == "checklist"
        refs = [e.ref for e in parse_checklist(str(fake.issues[epic]["description"]))]
        assert f"acme/widgets#{found}" in refs
        assert "sbx:task" in fake.issues[found]["labels"]
        plan = _no_drift_after(api, attached.json()["plan"], headers)
        # An issue adopted without our headings: its words were its goal.
        node = _node(plan, "Found on GitLab")
        assert node["goal"] == "Loose words."
        edited = _edit(api, headers, plan, node, context="Found while planning.")
        assert edited.status_code == 200, edited.text
        written = str(fake.issues[found]["description"])
        assert parse_sections(written) == {
            "goal": "Loose words.",
            "context": "Found while planning.",
        }
        plan = _no_drift_after(api, edited.json(), headers)
        detached = _detach(api, headers, plan, _node(plan, "Found on GitLab")["id"])
        assert detached.status_code == 200, detached.text
        refs = [e.ref for e in parse_checklist(str(fake.issues[epic]["description"]))]
        assert f"acme/widgets#{found}" not in refs and len(refs) == 2
        assert fake.issues[found]["state"] == "opened" and fake.issues_closed == []
        _no_drift_after(api, detached.json(), headers)

    def test_a_forge_edit_since_the_read_is_refused_and_not_written(self, api: Api) -> None:
        fake, plan, headers = _gitlab(api)
        a = _number(plan, "A")
        fake.person_edits(a, title="A, on GitLab")
        before = str(fake.issues[a]["description"])
        refused = _edit(api, headers, plan, _node(plan, "A"), goal="Mine.")
        assert refused.status_code == 409 and refused.json()["code"] == "forge_changed"
        assert refused.json()["current"]["title"] == "A, on GitLab"
        assert fake.issues[a]["description"] == before
        assert fake.issues[a]["title"] == "A, on GitLab"
