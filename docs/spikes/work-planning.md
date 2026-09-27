# Spike: planning work into the forge (initiative, epic, task)

Status: **proposed.** The product decisions below were taken with the
maintainer on 2026-09-26; nothing here is built yet. The effort spans three
repositories: sbxloop owns the contract, the planner, publishing,
reconciliation and epic runs; Angie builds the reference UX; Lantern ports
it. Code-seam claims were read off `main` at f87f6c5e. Forge behaviour not
exercised in this session is labelled **field-unverified** where it appears.

## The outcome

A person describes a larger effort — an initiative or an epic — in Angie or
Lantern, points it at a repository, and sbxloop helps them turn it into
issues the loop can work: it asks what it needs to know, reads the
repository, proposes a breakdown one level at a time, and, once a person has
edited and approved a level, writes it to the forge as linked issues. An
approved epic can then be run: its tasks are admitted as ordinary issue
runs, in dependency order, in parallel where they are independent.

Today sbxloop is very good at *processing* work and has no help at all for
*producing* it. Every issue it works was written by hand, on the forge, by
someone who had to know how big a run can be and what a good issue for this
loop looks like. This spike closes that gap without reopening the one the
1.0 cutover closed.

## Why now, and the constraint that shapes it

The pieces exist, scattered:

- **Issue writes.** `IssueOps.issue_create` and `label_create` exist on both
  backends (`vcs/protocol.py`, `vcs/github/ops.py`, `vcs/gitlab/ops.py`). The
  concierge, agent initiative, the workload `issue` sink and follow-ups all
  file issues today.
- **Decomposition.** `PhaseRunner.decompose()` (`engine/phases.py`) turns an
  outcome into a `TaskGraph` of `TaskSpec`s — id, title, description,
  `depends_on`, `acceptance_criteria`, `verify_commands` — but only inside a
  single run, and the graph never leaves it.
- **Parentage.** `WorkItem` already carries `parent_item_id` and
  `chain_depth` (`daemon/model.py`).
- **Clarifying questions.** `daemon/chat_choices.py` is a transport-free
  model for enumerable questions.
- **Human approval.** Gates (`POST /v1/gates/{id}/approve` with
  `expected_revision`) and `publish = "hold"` are the existing
  approve-before-act shapes.
- **Open-issue listing.** `GET /v1/repositories/{id}/issues` (2.1.18) backs
  Lantern's issue picker.

What does not exist: any notion of a hierarchy above an issue, any write of
an issue's body after creation, sub-issues, milestones, projects, or a way
for a client to ask for a breakdown.

**The constraint.** The 1.0 cutover removed every path by which the loop
filed its own work, because self-filed issues driving the loop forward had
become a spiral (`engine/followups.py` header). Everything below keeps the
rule that made that fix hold: **nothing reaches the forge, and nothing
reaches the queue, without a person doing it.** The planner proposes; a
person publishes; a person starts an epic run. Agents hold none of the new
capabilities.

## Decisions

| Question             | Decision                                                                                                                                                                                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Forge shape          | Every level is an issue. On GitHub, children are native sub-issues and a level label marks the level. On GitLab, always level labels plus a managed checklist of children in the parent's body — even on a tier with native epics, for one consistent shape. |
| Approval             | Per level. The planner proposes one level (an initiative's epics, or an epic's tasks); a person edits and publishes that level; only then can the next level be broken down.                                                                                 |
| Where the tree lives | An sbxloop plan record (drafts, answers, generation context, history) synced to the forge on publish. After publish **the forge wins**: sbxloop reconciles edits made there, and a re-plan proposes a diff a person approves.                                |
| Forges               | GitHub and GitLab at launch. Gitea fails closed with `BackendNotImplemented`, as today.                                                                                                                                                                      |
| Repositories         | An initiative lives in one home repository. Each epic targets exactly one repository, which may differ from the home. Tasks live in their epic's repository.                                                                                                 |
| Input                | A structured form, then a clarifying interview when the planner judges the input underspecified. (Promoting an existing issue is deferred; see Non-goals.)                                                                                                   |
| Grounding            | The planner runs as an agent job in the sandbox with a read-only checkout of the target repository, so tasks name real files and real verify commands.                                                                                                       |
| Task size            | One task is one run and one pull request, carrying acceptance criteria, verify commands and `depends_on`. The in-run decompose still splits a task into internal steps; it never widens the PR.                                                              |
| Execution            | "Run epic" admits the epic's tasks as issue runs in dependency order, in parallel when ready, bounded by the existing queue and usage limits. A failure pauses its dependents, not the epic. Any task can still be started alone from the issue picker.      |
| Permissions          | Two new principal capabilities: `plans:create` (draft, generate, edit) and `plans:publish` (write to the forge, start an epic run).                                                                                                                          |
| Entry level          | Any. A plan may start at an initiative (epics, then tasks) or at a lone epic (tasks). A lone epic can later be attached to an initiative.                                                                                                                    |
| Guardrails           | Human publish, plus `[planning]` caps on epics per initiative and tasks per epic (defaults 8 and 12). Re-plans are diffs, deduplicated against existing children by marker.                                                                                  |
| Client placement     | A new primary "Plans" section next to Tasks in Angie and Lantern, capability-gated.                                                                                                                                                                          |

## Model

A **plan** is a tree of **nodes**. A node has a `level` (`initiative`,
`epic`, `task`), a `repository` (for an initiative, its home), and a body
with named sections so both the forge rendering and the run's decompose can
read it:

- `title`
- `goal` — the outcome, in the reader's words
- `context` — what the planner found in the repository that matters
- `acceptance_criteria` — list; required on tasks
- `verify_commands` — list; tasks only, authored under the same rules
  `decompose.md` already enforces (workspace root, no shell variables)
- `depends_on` — sibling node ids; tasks only
- `non_goals`, `constraints` — optional

A task's sections are deliberately a superset of `TaskSpec`, so a task issue
is exactly the brief a run wants: when the run's decompose reads the issue,
it finds acceptance criteria and verify commands already written, and its
job shrinks to ordering the internal steps.

A node moves through `draft → proposed → approved → published`, then
follows the forge (`open`, `closed`). `proposed` means the planner wrote it
and no person has touched it; editing a proposed node makes it `draft`
again; `approved` is a person's "this is right" before publish. Nodes a
person adds on the forge after publish are adopted as `published` with
`origin = forge`.

Every write bumps a `revision`; every mutating call takes
`expected_revision`, as runs and gates already do.

Plans are stored in the daemon's state store, beside items and runs (a new
migration). Angie and Lantern hold no plan state of their own.

## Forge representation

Every node, once published, is one issue. Its body is the rendered sections
plus a hidden marker — `<!-- sbx-plan: <plan_id>/<node_id> -->` — which is
how publish stays idempotent, how reconciliation finds the tree, and how a
re-plan deduplicates, the same device follow-ups use across runs.

Publishing never applies the trigger label or the workload label. A
published task is inert until a person starts it.

### GitHub

- **Level.** A label per level (`sbx:initiative`, `sbx:epic`, `sbx:task`),
  added to the repository's `LabelSet` so `repositories/labels/sync`
  creates them with the lifecycle labels. Issue types are organisation-only
  and are not used.
- **Hierarchy.** Native sub-issues: `POST /repos/{owner}/{repo}/issues/{number}/sub_issues` with the child's issue
  *id* (not number), plus list and remove. An initiative's epics in other
  repositories are cross-repository sub-issues — **field-unverified** for a
  personal account and for a GitHub App installation that does not cover
  both repositories; if the forge refuses, the epic is still published and
  the initiative's body carries it in a managed checklist, with the reason
  named in the plan.
- **Dependencies.** `depends_on` is rendered into the task body as a
  "Depends on" list of issue references; that list is the source of truth.
  GitHub's native issue dependencies are written too where the forge
  answers `supported` — **field-unverified**, and optional.
- **Limits.** GitHub caps children per parent and nesting depth; the
  `[planning]` caps sit well under both. **field-unverified** exact values.

### GitLab

- **Level.** Plain labels with the same names. Scoped labels (`sbx::epic`)
  are Premium and are not used.
- **Hierarchy.** Always a managed block in the parent's description,
  between `<!-- sbx-plan:children -->` markers, one `- [ ] group/project#N title` line per child. sbxloop rewrites only that block; everything
  outside it belongs to people. Closing a child ticks its line on the next
  reconcile. GitLab native epics and work-item hierarchy are not used even
  where available (decision above).
- **Dependencies.** The same "Depends on" list in the body.

### New forge operations

`IssueOps` gains what publishing and reconciliation need, on both backends,
each a named operation (no `raw()` use outside the backend):

- `issue_update(repo, number, *, title=None, body=None)` — nothing edits an
  issue body today.
- `sub_issue_add`, `sub_issue_remove`, `sub_issues_list` — GitHub only.
- A new forge capability `sub_issues` in the `CAPABILITIES` tuple, answered
  `supported` by GitHub and `unsupported` by GitLab (a policy, not a
  detection), which is what selects native links or the checklist.
- `tests/fakes/fake_github.py` gains sub-issues, in the same PR as the ops.

## The planner

A plan generation is an agent job in the sandbox pair with a read-only
checkout of the node's repository (for an initiative, a shallow read of the
home repository plus the README and instruction files of each repository an
epic targets). It never gets write credentials and never touches the forge;
publishing is a separate host action.

It runs in two steps, each one agent turn, each validated against a
pydantic model with the same retry `decompose()` uses:

1. **Clarify.** Given the node and any prior answers, return either `ready`
   or up to `max_questions` questions in the `ChoiceQuestion` shape (two to
   five choices plus free text), so every client renders them the way chat
   already does. The plan parks in `awaiting_answers` until a person answers
   or skips.
2. **Propose.** Return the node's children as `proposed` nodes, at most the
   level's cap, each with the sections above. For a re-plan of a published
   node, the input includes its current children and the output is a diff —
   `add`, `modify`, `suggest_close` — never a replacement; children matched
   by marker are never duplicated.

Prompts live in `engine/prompts/plan_clarify.md` and `plan_propose.md`,
under the existing template contract and the domain-neutrality gate. The
model is a new `AgentModels.plan` entry. Generation spend is metered to the
usage pool and shows in Usage like any run.

**Open question for step 1 of the build:** whether a generation is a fourth
`RunKind` (gaining the chronology, steering and cancel for free, at the cost
of touching the run shape the trail fixture pins) or an operation under
`/v1/operations` with its own events. This spike leans to an operation: a
generation delivers nothing and has no stages, and cancel is all it needs.

## Publishing

`POST /v1/plans/{id}/nodes/{node_id}/publish` publishes the node's
`approved` children — one level — and, if the node itself is unpublished,
the node first. For each child, in order: look for its marker on the forge
(an earlier attempt may have written it), create the issue if absent, add
labels, link it (sub-issue or checklist), and record the forge reference.
A partial failure leaves the plan with per-node results and the call can be
repeated; it resumes, it does not duplicate. The call takes an
`Idempotency-Key`, like `POST /v1/items`.

Publishing requires `plans:publish` and the target repository to be
enabled. A repository whose forge cannot hold a plan (Gitea) is refused
with the named reason.

## Reconciliation: the forge wins

After publish, the forge is the record. sbxloop re-reads a plan's tree when
a client opens it, when a client asks (`POST /v1/plans/{id}/sync`), and on a
slow poll for plans with an active epic run. For each node it folds in the
forge's title, body sections, state and children:

- An edit on the forge updates the node and is shown as "changed on the
  forge" until someone looks.
- A child added on the forge is adopted with `origin = forge`.
- A child removed from its parent, or a deleted issue, detaches the node and
  says so; nothing is recreated.
- A managed section a person broke (a mangled checklist block, a removed
  marker) is reported, not repaired silently.

sbxloop never writes over a forge edit. The only writes after publish are a
person's approved re-plan diff, the managed checklist block on GitLab, and
the epic's completion comment.

## Running an epic

`POST /v1/plans/{id}/nodes/{epic_id}/run` starts an **epic run**, owned by
the daemon:

- The ready set is the epic's open tasks whose `depends_on` are all closed.
- Each ready task is admitted through the existing issue admission
  (`IssueAdmission`, item `gh:issue:<n>`, or the GitLab equivalent) with
  `parent_item_id` naming the epic run — not by applying the trigger label,
  so no poll-driven path is added. The usual lifecycle labels follow from
  the claim.
- Independent tasks run concurrently, bounded by the queue, holds and the
  usage pool exactly as any other item is.
- A run that lands closes its issue through the normal "Closes" path, which
  makes its dependents ready.
- A failed or blocked task pauses its dependents; siblings continue. A
  person can retry the task, skip it (treat as done), or stop the epic run.
- When every task is closed, sbxloop comments a summary on the epic issue
  and closes it.

Epic runs require `plans:publish` — they are the one path that turns plan
content into queued work, so they sit with publishing, not with
`items:create`. Agent principals hold neither capability; agent initiative
and its `max_chain_depth` are untouched.

## Contract

### Features and capabilities

`GET /v1/capabilities` `features` gains, each conditional on the forge
backends configured:

- `planning` — plans, nodes, publish, reconcile
- `planning.clarify` — the clarifying step and answers endpoint
- `planning.run` — epic runs

The principal `Capability` literal gains `plans:create` and
`plans:publish`. Defaults: members hold `plans:create`; admins and owners
hold both. Reading plans needs `runs:read`.

Each repository in `/v1/repositories` gains `planning: {hierarchy: "native" | "checklist" | "unsupported", reason}` so a client can say, before
anyone types, what the plan will look like on that forge.

### Routes

| Route                                                                 | Purpose                                                         |
| --------------------------------------------------------------------- | --------------------------------------------------------------- |
| `GET /v1/plans`, `POST /v1/plans`                                     | List (filter by repository, level, state); create from the form |
| `GET`, `PATCH`, `DELETE /v1/plans/{id}`                               | Read the tree; edit; delete a draft or archive a published plan |
| `POST /v1/plans/{id}/nodes`, `PATCH`, `DELETE .../nodes/{node_id}`    | Add, edit, reorder, remove unpublished nodes                    |
| `POST .../nodes/{node_id}/breakdown`                                  | Start a generation (clarify, then propose) for a node           |
| `POST .../nodes/{node_id}/answers`                                    | Answer or skip clarifying questions                             |
| `POST .../nodes/{node_id}/approve`                                    | Mark proposed or draft children approved                        |
| `POST .../nodes/{node_id}/publish`                                    | Publish one level                                               |
| `POST /v1/plans/{id}/sync`                                            | Reconcile now                                                   |
| `POST .../nodes/{epic_id}/run`, `.../run/pause`, `/resume`, `/cancel` | Epic run control                                                |
| `POST .../nodes/{task_id}/run/retry`, `.../run/skip`                  | Per-task recovery in an epic run                                |

### Events

`plan.created`, `plan.node.changed`, `plan.generation.started`,
`plan.generation.questions`, `plan.generation.proposed`,
`plan.generation.failed`, `plan.published`, `plan.drift`,
`plan.run.started`, `plan.run.task_admitted`, `plan.run.paused`,
`plan.run.completed`. They ride `/v1/events`, the SSE stream and scoped
events. The push dispatcher gains three notices: questions waiting for you,
a proposal ready for you, and an epic run you started paused.

### Config

A `[planning]` block, with a per-repository override where `RepoConfig`
narrows, in the config model, the example config and the user-guide knob
table:

| Key                        | Default | Meaning                                  |
| -------------------------- | ------- | ---------------------------------------- |
| `enabled`                  | `true`  | Offer planning on configured forges      |
| `max_epics_per_initiative` | `8`     | Cap on one initiative's epics            |
| `max_tasks_per_epic`       | `12`    | Cap on one epic's tasks                  |
| `max_questions`            | `5`     | Clarifying questions per generation      |
| `close_completed_epics`    | `true`  | Comment and close an epic when it's done |

Plus `[agent.models] plan`, and the three level labels in `LabelSet`.

## Client UX (Angie first, Lantern ports it)

Both clients gate the section on `planning` and name the reason when it is
absent ("the server needs `planning`", "this repository's forge can't hold
plans: Gitea is not supported").

- **Plans list.** Initiatives and lone epics with repository, level, a
  rollup (tasks closed of total, running, failed), an active-run indicator
  and a drift badge.
- **New plan.** Level (initiative or epic), title, goal, success criteria,
  constraints and non-goals, repository (the home, for an initiative), and
  attachments through the existing file inputs.
- **Breakdown.** "Break down" starts a generation with live progress. A
  questions card appears if the planner asks. The proposal is an editable
  list of children: edit any section, reorder, delete, add, set
  dependencies, regenerate one child or the whole level with a note. The
  primary action names what it will do — "Publish 5 epics to
  brettbergin/sbxloop" — and nothing is written before it.
- **Published node.** Forge link and state, children, drift shown as a diff
  against the last known version, and "Re-plan", which shows a proposed diff
  for approval.
- **Epic run.** "Run epic" on a published epic, then a dependency-ordered
  list of tasks with their state (ready, queued, running, landed, failed,
  blocked by a dependency) linking to each run's thread; pause, resume,
  stop, and retry or skip on a failed task.
- **Action center.** Questions waiting, proposals ready, and paused epic
  runs appear as decisions beside gates.
- **Tasks.** The issue picker shows level labels, so plan tasks are easy to
  start one at a time.

Lantern keeps the behaviour and uses the idiom: the form is a sheet, the
tree is a disclosure list, delete and regenerate are swipe actions, the
iPad uses a split view, and the three notices arrive as push. The fake
server and demo world gain plan routes and a seeded plan, so every screen
works in demo mode.

## Build sequence

Each step is a PR; the epics and tasks in the appendix are this sequence as
issues.

1. Forge ops: `issue_update`, sub-issues and the `sub_issues` capability,
   the GitLab checklist writer, the fake.
2. Plan store, `/v1/plans` CRUD, capabilities, features, OpenAPI.
3. Publish per level, idempotent by marker.
4. Planner: config, prompts, generation job, clarify and answers.
5. Reconciliation and re-plan diffs.
6. Epic runs.
7. Angie: API types and gating, list and form, breakdown editor and
   publish, published view and re-plan, epic run and action center.
8. Lantern: regenerated client, capabilities, fake and demo world, then the
   same screens, push categories and `docs/parity.md` rows.

sbxloop steps 1–3 unblock the clients' read and publish screens; the
clients can build against the fakes while 4–6 land.

## Non-goals for the first version

- Promoting an existing issue into an epic or initiative.
- Gitea; GitHub Projects and milestones; GitLab native epics.
- Tasks that span repositories.
- Publishing or running anything without a person's action; agent-started
  plans.
- Two-way sync of arbitrary body edits back into the plan's structured
  sections beyond the rendered headings.

## Field-unverified

- Cross-repository sub-issues on a personal account, and under a GitHub App
  installation that does not cover both repositories.
- GitHub's exact caps on sub-issues per parent and nesting depth.
- GitHub native issue dependencies: availability and API shape.
- GitLab description size limits for a large managed checklist.
- Secondary rate limits while publishing a full level in one call.

## Appendix: this effort as issues

Filed only after the maintainer confirms. The initiative is in sbxloop; each
epic lives in its repository and is a sub-issue of the initiative.

**Initiative (sbxloop):** Plan work into the forge from the apps
(initiative, epic, task).

**Epic A (sbxloop): Plans API and forge publishing**

1. Add `issue_update` and sub-issue operations to the forge protocol, with a
   `sub_issues` capability and fake support.
2. Write a managed children checklist into GitLab parent issues.
3. Store plans and serve `/v1/plans` with `plans:create` and
   `plans:publish`.
4. Publish one plan level to the forge, idempotent by marker.
5. Reconcile a published plan from the forge and report drift.

**Epic B (sbxloop): The planner**

1. Add the `[planning]` config and the `plan` agent model.
2. Generate a proposed level in the sandbox from a read-only checkout.
3. Ask clarifying questions before proposing, and take answers.
4. Re-plan a published node as an approved diff.

**Epic C (sbxloop): Epic runs**

1. Admit an epic's ready tasks as issue runs in dependency order.
2. Pause dependents of a failed task, with retry, skip and stop.
3. Close a completed epic with a summary, and send the planning push
   notices.

**Epic D (angie): Plans section**

1. Add plan types, capability gating and daemon fixtures.
2. Build the Plans list and the new-plan form.
3. Build the breakdown editor with clarifying questions and per-level
   publish.
4. Show drift on published plans and review re-plan diffs.
5. Run an epic from Plans and surface planning decisions in the action
   center.

**Epic E (lantern): Plans section**

1. Regenerate the client and add planning capabilities, fake routes and a
   demo plan.
2. Build the Plans list and the new-plan sheet.
3. Build the breakdown editor with clarifying questions and per-level
   publish.
4. Show drift, review re-plans and run epics, with action-center decisions
   and push categories.
5. Record the planning rows in `docs/parity.md` and walk the screens against
   a real daemon.
