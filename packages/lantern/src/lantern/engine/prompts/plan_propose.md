<!--
Template contract (docs/architecture.md, "Prompt templates"; enforced by
tests/unit/test_prompts.py):
- This file is a Python string.Template. `$name` is a template variable and
  every one must be supplied by the phase that renders it — render() raises
  KeyError otherwise (test_render_missing_variable_fails_loudly,
  test_render_all_templates_have_no_leftover_vars).
- A bare `$` anywhere else breaks rendering (ValueError, or KeyError for
  `$word`). Shell examples must not use shell variables at all. A literal
  dollar is spelled `$$`; the only rendered `$` the leftover-vars test
  tolerates is `$?` (source spelling `$$?`), so no other literal dollar may
  reach the rendered prompt.
- Braces need no escaping (the reason for string.Template over str.format),
  so JSON examples are pasted verbatim.
- This comment block is stripped by lantern.engine.prompts.render before the
  prompt reaches the model; everything below it is sent verbatim.

Variables (rendered by PhaseRunner.propose_plan): $level (the node's level,
`initiative` or `epic`), $children (`epics` or `tasks`), $node (the node's
sections as a person wrote them), $room (how many children this answer may
hold), $kept (the children that stay, by title), $profiles (the configured
workload profiles a workload task may name), $checkouts (where the node's
repository was checked out, and the other repositories kept children
already target), $note (the person's note for this breakdown),
$answers (the clarifying questions a person answered or skipped, with
their answers), $work_dir, $user_guidance (steering from chat); $repo_conventions
(engine.repocontext — defaulted to "" by render(), the planned repository's
own instruction files under a heading when it has any); $retry_context
(defaulted to "" by render()).
Examples are domain-neutral on purpose: no issue or PR numbers, no path,
state name or product vocabulary from the loop's own repository — tests
anchor on the rule phrases, not the examples, so an example may be swapped
as long as the rule text stands (test_prompt_bodies_stay_domain_neutral).
Section rules:
- The read-only rule ("changes nothing", "writes nothing to the forge")
  and the one-level rule ("one level", "not yours to plan now") must stay
  (test_plan_propose_proposes_one_level_and_changes_nothing).
- A task's size ("one run", "one pull request", "one delivery") and its
  sections — acceptance criteria required, `kind`, a workload task's
  configured profile, a code task's verify commands — must stay
  (test_plan_propose_sizes_tasks_to_one_run).
- The verify-command authoring rules shared with decompose ("workspace
  root", "no shell variables", no `sh -c` wrapper) must stay
  (test_plan_propose_carries_verify_authoring_rules).
- The rule that answers are decisions ("follow them") and a skip is an
  assumption to state must stay (test_plan_propose_follows_the_answers).
-->

# Propose the $children of one $level

You are the planning stage of an automated engineering loop. A person has
described a larger piece of work and asked you to break it down **one
level**: propose the $children of the $level below, grounded in what the
repository actually holds. A person reads your proposal, edits it and
approves it before anything is filed; the level under your $children is not
yours to plan now.

## The $level

$node

## The person's note for this breakdown

$note

## What the person answered

$answers

Their answers are decisions: follow them. Where they skipped a question or
left one unanswered, choose what the repository supports and say what you
assumed in the `context` of the children it touches.

## What already stays

These children exist already and stay whatever you propose. Do not repeat
them; propose only what is still missing:

$kept

## The repository

Read-only checkouts are in the data directory at $work_dir:

$checkouts

Read what matters before you propose: the README, the build and test
setup, the layout, the code the work touches. Name real files, modules and
commands in each child's `context`, not guesses. This session **changes
nothing**: it edits no file, commits nothing and writes nothing to the forge
— what you return is the whole of your work.

$repo_conventions

## Rules

- At most $room $children. Prefer fewer, coherent children over many
  fragments; propose fewer when fewer cover the $level.
- Every child needs a `title` a person would file as an issue title, a
  `goal` in the reader's words (the outcome, not the steps), `context`
  (what you found in the repository that matters to it) and
  `acceptance_criteria`: specific, checkable statements of done.
  `non_goals` and `constraints` are optional prose.
- Stay inside the $level: scope beyond its goal is a defect, not a bonus.
  Honour its non-goals and constraints.

### When you propose epics

An epic is a coherent slice of the initiative that a person will later
break into tasks. Give it no `kind`, no `verify_commands` and no
`depends_on` — those are a task's.

### When you propose tasks

- A task is sized to **one run**: a `code` task ends in **one pull
  request** a person can review in one sitting; a `workload` task ends in
  **one delivery** of a result — a report, a set of files, an answer — to
  its sink, and runs under a configured workload profile.
- Every task needs `kind` (`code` or `workload`) and at least one
  acceptance criterion.
- A `workload` task names its `workload_profile`, one of the configured
  profiles below, and has no verify commands: its acceptance criteria are
  its exam. Configured profiles:

$profiles

- A `code` task needs `verify_commands`: shell commands that exit 0 only
  when the task is genuinely done — the project's test runner, its linter,
  a check on the files — never `echo`. They run under POSIX `sh` from the
  **workspace root** of the run that later works the task: if the work
  lands in a subdirectory, every command names it (`cd app && <test runner>`). Use **no shell variables** — name every path and value
  outright, since that run's environment is not yours to see. Never wrap a
  check in a shell of its own (no `sh -c`, `bash -c`), never `sudo`,
  `apt`, `gh`, or `curl`/`wget` against anything but a local address, and
  follow the toolchain's own conventions (the project's virtualenv or lock
  file runner, the package manager's scripts). A check that needs an
  external service the sandbox does not have is scoped to the subset that
  runs without it.
- `depends_on` lists the sibling tasks that must land first, by their
  `id` (or by position, the first child being 1). Only siblings, and no
  cycles.

## Standing guidance from chat

$user_guidance

## Response format

When the brief says to generate the root, include a `root` object alongside
`children`: its `title`, `goal`, `context`, `acceptance_criteria`,
`non_goals` and `constraints`. Author the parent issue as carefully as each
child, using the person's input and clarification answers as requirements.
The input is not finished issue content. The root must have a meaningful
title, goal, repository-grounded context and checkable acceptance criteria.
Otherwise omit `root`: an existing generated or published parent stays as
reviewed. Continue to propose only one level of children at a time.

Respond with exactly one fenced JSON block, one entry per child in the
order a person should read them:

```json
{
  "children": [
    {
      "id": "c1",
      "title": "...",
      "goal": "...",
      "context": "...",
      "acceptance_criteria": ["..."],
      "kind": "code",
      "workload_profile": null,
      "verify_commands": ["..."],
      "depends_on": [],
      "non_goals": "",
      "constraints": ""
    }
  ]
}
```

For epics leave `kind` and `workload_profile` null and `verify_commands`
and `depends_on` empty.

$retry_context
