"""The daemon's tables: the queue, the ledger, what it is holding, and what it spent.

The same rules as :mod:`lantern.db.engine_models` apply — this describes the
schema that is on disk rather than the one anyone would design now, because
a deployed ``state.db`` has to keep opening (#539). ``REAL`` rather than
``Float``, and ``nullable=True`` on the primary keys the hand-written DDL
declared inline without ``NOT NULL``, for the reasons documented there.

Three shapes here are worth knowing:

* **Work items are keyed twice.** ``item_id`` is the primary key, but
  ``UNIQUE(source_key, repo)`` is the one that matters: without ``repo`` in
  it, two configured repositories with an issue of the same number collide.
  That constraint is why the multi-repo upgrade had to rebuild the table —
  SQLite cannot drop a constraint with ``ALTER``.
* **Chat state is keyed by backend.** A run can have a thread on Discord or
  Slack *and* one on the operator console's local bridge at the same time,
  so ``daemon_chat_threads`` and ``daemon_gate_prompts`` are keyed
  ``(run_id, backend)`` and ``daemon_run_watches`` carries ``backend`` in
  its unique key.
* **JSON lives in TEXT columns**, serialised in Python: ``notify_ids`` on
  both hold tables, ``reactions_json`` on local messages. ``embed_json`` and
  ``choices_json`` are opaque even to the store — the bridge layer owns
  their contents.
"""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

import lantern.db.work_marks  # noqa: F401 - registers the mark triggers on Base
from lantern.db.base import Base
from lantern.db.revisions import attach as attach_revision_trigger

#: ``text`` under a second name. ``LocalMessageRow`` has a column called
#: ``text``, which shadows the import for every line after it in that class
#: body — including the ``server_default``s that need it.
sql_text = text


class WorkItemRow(Base):
    """One thing the daemon has been asked to do, and how far it has got.

    ``item_id`` is a typed id (``gh:issue:12``, ``chat:<message id>``).
    Rows written before typed ids carry the bare ``gh:12`` form and are
    matched by either spelling on read — parsing is lenient, rendering is
    strict — so no migration of existing rows was ever needed.
    """

    __tablename__ = "daemon_work_items"
    __table_args__ = (
        # The key that actually guards correctness; see the module docstring.
        UniqueConstraint("source_key", "repo"),
        Index("idx_daemon_items_state", "state", "created_at"),
        # What the chat projection joins work to its conversation on
        # (revision 0032). It used to compare the message id against a
        # prefix of ``source_key``: a function on a column, which SQLite
        # cannot index, so every delivery read cost one pass over the
        # messages table per work item.
        Index("idx_daemon_items_message", "message_id"),
        # A breakdown asks whether its node already has a generation
        # queued or running (revision 0044).
        Index("idx_daemon_items_plan_node", "plan_node_id"),
    )

    item_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    url: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    state: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    claimed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    run_id: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    pending_report: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(Text)
    # Empty for a row written before multi-repo; settled at daemon startup
    # rather than claimed by whichever repository is polled first.
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    # Earliest the item may be dispatched — a scheduled retry's backoff.
    not_before: Mapped[float | None] = mapped_column(REAL)
    # Held across the two steps of a claim, so a daemon killed mid-claim
    # does not orphan the issue.
    claim_token: Mapped[str | None] = mapped_column(Text)
    # What a previous attempt left behind, so a retry reuses its branch and
    # pull request instead of opening a second one.
    prior_run_id: Mapped[str | None] = mapped_column(Text)
    prior_branch: Mapped[str | None] = mapped_column(Text)
    prior_pr_number: Mapped[int | None] = mapped_column(Integer)
    run_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'code'"))
    profile: Mapped[str | None] = mapped_column(Text)
    recipe: Mapped[str | None] = mapped_column(Text)
    recipe_target: Mapped[str | None] = mapped_column(Text)
    # Bumped by a trigger on every UPDATE (revision 0010): what a remote
    # command's `expected_revision` is checked against.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    # Who the work was admitted for (revision 0027): the channel it answers
    # to, the lead asked for, the agent assignment (the roles requested at
    # admission, then the plan dispatch made from them), and, for work an
    # agent started, that agent, the item it came from and how deep the
    # chain of agent-started work is.
    channel_id: Mapped[str | None] = mapped_column(Text)
    lead_agent: Mapped[str | None] = mapped_column(Text)
    assignment_json: Mapped[str | None] = mapped_column(Text)
    origin_agent: Mapped[str | None] = mapped_column(Text)
    parent_item_id: Mapped[str | None] = mapped_column(Text)
    chain_depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    # The chat message that asked for this work (revision 0032), derived
    # from ``source_key`` at insert and backfilled for the rows that
    # predate the column. NULL for work an issue, a schedule or an inbox
    # file asked for: those name no message and link by channel instead.
    message_id: Mapped[str | None] = mapped_column(Text)
    # The plan node a `plan` item proposes the next level of (revision
    # 0044); NULL for every other kind.
    plan_id: Mapped[str | None] = mapped_column(Text)
    plan_node_id: Mapped[str | None] = mapped_column(Text)


class DaemonRunRow(Base):
    """The ledger: one row per run the daemon started, and how it ended."""

    __tablename__ = "daemon_runs"
    __table_args__ = (
        Index("idx_daemon_runs_started", "started_at"),
        Index("idx_daemon_runs_item_started", "item_id", "started_at", "run_id"),
    )

    run_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[float] = mapped_column(REAL, nullable=False)
    finished_at: Mapped[float | None] = mapped_column(REAL)
    result: Mapped[str | None] = mapped_column(Text)


class RunResumeRow(Base):
    """One resume of one run. Counted against the per-item resume budget."""

    __tablename__ = "daemon_run_resumes"
    # AUTOINCREMENT on disk (the baseline wrote it): ids are never reused,
    # which readers that track a high-water mark rely on.
    __table_args__ = (
        Index("idx_daemon_resumes_at", "resumed_at"),
        Index("idx_daemon_resumes_item", "item_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    resumed_at: Mapped[float] = mapped_column(REAL, nullable=False)


class DaemonStateRow(Base):
    """Key/value for everything that is not worth a table of its own.

    The schema version the console handshakes on, the circuit breaker, the
    per-repository health the doctor reads, the local bridge's heartbeat.
    """

    __tablename__ = "daemon_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    value: Mapped[str | None] = mapped_column(Text)


class RunWatchRow(Base):
    """Who asked to be told when a run finishes, per backend."""

    __tablename__ = "daemon_run_watches"
    __table_args__ = (
        UniqueConstraint("run_id", "watcher_id", "backend"),
        Index("idx_daemon_run_watches_run", "run_id"),
    )

    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    watcher_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'discord'"))

    # The table has no PRIMARY KEY on disk — only the UNIQUE above — and it
    # must stay that way, so the mapper is told what identifies a row
    # instead of the DDL being changed to say it.
    __mapper_args__ = {  # noqa: RUF012 - SQLAlchemy's own contract for this
        "primary_key": [run_id, watcher_id, backend]
    }


class RequesterRow(Base):
    """Who asked for a piece of work, so the answer goes back to them."""

    __tablename__ = "daemon_requesters"
    __table_args__ = (PrimaryKeyConstraint("source_key", "repo"),)

    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    requester_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


class PriorAttemptRow(Base):
    """What the last attempt on this item left behind, kept past the item.

    Survives a row being requeued or superseded, which is what lets a fresh
    attempt adopt the branch and pull request the previous one opened.
    """

    __tablename__ = "daemon_prior_attempts"
    __table_args__ = (PrimaryKeyConstraint("source_key", "repo"),)

    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    run_id: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False, server_default=sql_text("0"))


class ChatThreadRow(Base):
    """The thread a run's chronology is written to, on one backend."""

    __tablename__ = "daemon_chat_threads"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "backend"),
        Index("idx_chat_threads_thread", "thread_id"),
    )

    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'discord'"))
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The headline card and the status line inside the thread, edited in
    # place as the run moves.
    headline_id: Mapped[str | None] = mapped_column(Text)
    status_id: Mapped[str | None] = mapped_column(Text)


class MergeGateRow(Base):
    """A run parked until a person approves the merge.

    ``state`` moves ``open`` to ``approving`` to a terminal value, and the
    move to ``approving`` is a compare-and-set: two people pressing the
    button at once means one of them loses the CAS rather than both merging.
    """

    __tablename__ = "daemon_merge_gates"
    __table_args__ = (
        Index("idx_merge_gates_state", "state"),
        Index("idx_merge_gates_item", "item_id"),
    )

    run_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_url: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    branch: Mapped[str | None] = mapped_column(Text)
    notify_ids: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'[]'"))
    custom_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'open'"))
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'merge'"))
    # Where the prompt was posted, before it moved to `daemon_gate_prompts`
    # so it could exist once per backend. Still readable, never written
    # again — an older daemon in a rollback window may write them, and the
    # next start carries them across.
    prompt_channel_id: Mapped[str | None] = mapped_column(Text)
    prompt_message_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    resolved_at: Mapped[float | None] = mapped_column(REAL)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))


class GatePromptRow(Base):
    """Where a run's gate prompt is showing, one row per backend."""

    __tablename__ = "daemon_gate_prompts"
    __table_args__ = (PrimaryKeyConstraint("run_id", "backend"),)

    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)


class ReviewHoldRow(Base):
    """A run parked because its base branch requires an approval.

    Polled rather than pushed, so it carries its own schedule:
    ``next_poll_at`` and ``polls`` are the backoff, ``since_at`` is what the
    poll asks GitHub about.
    """

    __tablename__ = "daemon_review_holds"
    __table_args__ = (
        Index("idx_review_holds_state", "state"),
        Index("idx_review_holds_item", "item_id"),
    )

    run_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_url: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    branch: Mapped[str | None] = mapped_column(Text)
    # Who requested the changes, and whether they were a bot — an automated
    # reviewer gets one answer, never a fix loop.
    login: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    is_bot: Mapped[int | None] = mapped_column(Integer)
    approvals_required: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("1")
    )
    # Held because a person drafted the pull request, rather than because
    # the base demands an approval.
    held_by_draft: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    notify_ids: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'[]'"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'open'"))
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    since_at: Mapped[float] = mapped_column(REAL, nullable=False)
    next_poll_at: Mapped[float] = mapped_column(REAL, nullable=False)
    polls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    resolved_at: Mapped[float | None] = mapped_column(REAL)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))


class HoldRow(Base):
    """A named pause hold, kept across restarts (revision 0010).

    Holds used to be a set in the loop's memory, so every restart came back
    unpaused and a deploy had to snapshot and re-take them. A hold now
    stands until the side that took it — or an operator's ``resume --all``
    — releases it; ``owner_display`` and ``via`` say whose it is.
    """

    __tablename__ = "daemon_holds"

    name: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(Text)
    owner_display: Mapped[str | None] = mapped_column(Text)
    via: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    operation_id: Mapped[str | None] = mapped_column(Text)


class PendingClarificationRow(Base):
    """A question the concierge asked that nobody has answered yet.

    Intake asks but never blocks: at ``deadline`` the item is filed on the
    stated ``assumption`` anyway.
    """

    __tablename__ = "daemon_pending_clarifications"
    # AUTOINCREMENT on disk (the baseline wrote it): ids are never reused,
    # which readers that track a high-water mark rely on.
    __table_args__ = (
        Index("idx_pending_clarify_due", "state", "deadline"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    backend: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'discord'"))
    channel_id: Mapped[str | None] = mapped_column(Text)
    prompt_message_id: Mapped[str | None] = mapped_column(Text)
    asker_id: Mapped[str | None] = mapped_column(Text)
    asker_name: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    assumption: Mapped[str] = mapped_column(Text, nullable=False)
    deadline: Mapped[float] = mapped_column(REAL, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'open'"))
    resolved_at: Mapped[float | None] = mapped_column(REAL)


class LocalMessageRow(Base):
    """The operator console's mailbox: one row per message, both directions.

    This is the local chat bridge. The console writes inbound rows on a
    connection of its own while the daemon reads them, which is why the
    pending index is partial — the daemon's poll is a lookup on the few
    rows that are inbound and not yet taken, not a scan of the channel.
    """

    __tablename__ = "daemon_local_messages"
    # AUTOINCREMENT on disk (the baseline wrote it): ids are never reused,
    # which readers that track a high-water mark rely on.
    __table_args__ = (
        Index("idx_local_messages_channel", "channel_id", "id"),
        Index("idx_local_messages_updated", "channel_id", "updated_at"),
        Index(
            "idx_local_messages_pending",
            "id",
            sqlite_where=text("direction = 'in' AND taken_at IS NULL"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'message'"))
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    # Opaque here: the bridge layer owns what is inside them.
    embed_json: Mapped[str | None] = mapped_column(Text)
    choices_json: Mapped[str | None] = mapped_column(Text)
    gate_run_id: Mapped[str | None] = mapped_column(Text)
    reply_to_id: Mapped[int | None] = mapped_column(Integer)
    mention_users: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    author_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'sbx'"))
    author_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'sbx'"))
    reactions_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'[]'")
    )
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    edited_at: Mapped[float | None] = mapped_column(REAL)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False, server_default=sql_text("0"))
    # When the daemon took an inbound message. NULL means still pending.
    taken_at: Mapped[float | None] = mapped_column(REAL)


class ScheduleRowModel(Base):
    """A recurring ask, and where its last firing got to.

    Schedules live here rather than in the config file because they are
    runtime state the concierge creates and removes; the ``[[schedules]]``
    config block is imported once at start and is legacy.
    """

    __tablename__ = "daemon_schedules"

    name: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    anchor: Mapped[float] = mapped_column(REAL, nullable=False)
    last_due: Mapped[float | None] = mapped_column(REAL)
    last_fired_at: Mapped[float | None] = mapped_column(REAL)
    last_item: Mapped[str | None] = mapped_column(Text)
    paused_by: Mapped[str | None] = mapped_column(Text)
    paused_at: Mapped[float | None] = mapped_column(REAL)
    # The schedule itself. NULL on a row written before it moved into the
    # database; the config import fills those in on the next start.
    profile: Mapped[str | None] = mapped_column(Text)
    ask: Mapped[str | None] = mapped_column(Text)
    every: Mapped[str | None] = mapped_column(Text)
    cron: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float | None] = mapped_column(REAL)


class RepositoryRow(Base):
    """One registered repository: where a repository is declared to the
    daemon, now that the file's ``[[vcs.repos]]`` entries are imported
    once and registrations change live.

    ``removed_at`` keeps a removed registration's row, so the file's copy
    of that name is not imported again at the next start.
    """

    __tablename__ = "daemon_repositories"

    repo: Mapped[str] = mapped_column(Text, primary_key=True)
    # The forge it lives on; NULL inherits `[vcs] kind`.
    kind: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))
    deliver_base: Mapped[str | None] = mapped_column(Text)
    # "config" (imported from lantern.toml) or "api".
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    removed_at: Mapped[float | None] = mapped_column(REAL)
    # What the repository's lantern labels looked like when they were last
    # read (#630): when, the names read for, and the ones it did not carry,
    # both as JSON arrays. NULL means never read — not "carries them all".
    labels_checked_at: Mapped[float | None] = mapped_column(REAL)
    labels_expected: Mapped[str | None] = mapped_column(Text)
    labels_missing: Mapped[str | None] = mapped_column(Text)


class WorkspaceUsageRow(Base):
    """One usage sample charged to the workspace budget pool: a run's
    ``agent.usage`` event or a chat turn's reported usage.

    Append-only. ``ts`` is when the charge landed, which is the day it
    counts toward; ``source`` is ``run`` or ``turn`` and ``ref_id`` the run
    id or the turn's message id. Token columns hold what the backend
    reported, zero for a figure it did not report.
    """

    __tablename__ = "workspace_usage"
    __table_args__ = (
        Index("idx_workspace_usage_ts", "ts"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(REAL, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    ref_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_slug: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )


# The revision triggers ride with the tables they bump, so a database built
# from the metadata carries them like a migrated one does.
attach_revision_trigger(WorkItemRow.__table__, "item_id")
attach_revision_trigger(MergeGateRow.__table__, "run_id")
attach_revision_trigger(ReviewHoldRow.__table__, "run_id")


class PlanRow(Base):
    """One plan (#2340): an initiative or a lone epic being broken down
    into issues. The tree's nodes are ``daemon_plan_nodes``; the root node
    carries the plan's own sections. Every plan is visible to the whole
    workspace, drafts included.

    ``revision`` is the plan's one counter: every write to the plan or any
    of its nodes bumps it, and every mutating call names the revision it
    read, so an edit made against a tree someone else has since changed is
    refused rather than merged blind.
    """

    __tablename__ = "daemon_plans"
    __table_args__ = (
        Index("idx_daemon_plans_updated", "updated_at"),
        Index("idx_daemon_plans_goal", "goal_id"),
    )

    plan_id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    root_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    # "active" or "archived"; a draft is a plan with nothing published.
    state: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_by_display: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))
    # When the forge was last read into the plan (#2342), and what stopped
    # the last attempt, if anything. Neither bumps the revision.
    reconciled_at: Mapped[float | None] = mapped_column(REAL)
    reconcile_error: Mapped[str | None] = mapped_column(Text)
    input_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'{}'"))
    # Whether the plan may move itself forward (revision 0051): "manual"
    # (a person takes every step) or "auto". Nothing sets "auto" but a
    # holder of plans:publish.
    advance: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'manual'"))
    # The goal the plan was proposed from; NULL for a plan a person drafted.
    goal_id: Mapped[str | None] = mapped_column(Text)


class PlanNodeRow(Base):
    """One node of a plan: an initiative, an epic or a task, with the
    named sections both the forge rendering and a run's decompose read.
    Lists (acceptance criteria, verify commands, dependencies) are JSON
    arrays in TEXT. ``forge_*`` stay NULL until the node is published."""

    __tablename__ = "daemon_plan_nodes"
    __table_args__ = (Index("idx_daemon_plan_nodes_plan", "plan_id", "parent_id", "position"),)

    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    level: Mapped[str] = mapped_column(Text, nullable=False)
    repository: Mapped[str] = mapped_column(Text, nullable=False)
    # draft, proposed, approved or published.
    state: Mapped[str] = mapped_column(Text, nullable=False)
    # person, planner or forge.
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    context: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    acceptance_criteria_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'[]'")
    )
    kind: Mapped[str | None] = mapped_column(Text)
    workload_profile: Mapped[str | None] = mapped_column(Text)
    verify_commands_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'[]'")
    )
    depends_on_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'[]'")
    )
    non_goals: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    constraints: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    forge_number: Mapped[int | None] = mapped_column(Integer)
    forge_url: Mapped[str | None] = mapped_column(Text)
    # open or closed, as the forge last said.
    forge_state: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    # What reconciliation last read (#2342): the issue's updated_at, why the
    # node no longer follows its issue, whether its marker is gone, why its
    # managed children checklist could not be read.
    forge_updated_at: Mapped[str | None] = mapped_column(Text)
    forge_detached: Mapped[str | None] = mapped_column(Text)
    forge_marker_missing: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    forge_checklist_error: Mapped[str | None] = mapped_column(Text)
    # The forge's changes nobody has marked seen, as a JSON array.
    drift_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'[]'"))
    # The node's latest clarifying questions and a person's answers, as
    # JSON (revision 0045): which run asked, what, and what was answered or
    # skipped. NULL until a breakdown of the node asks something.
    generation_json: Mapped[str | None] = mapped_column(Text)
    # A re-plan's diff waiting for a person (#2346, revision 0046): the run
    # that proposed it and its entries, as a JSON object; NULL when none is
    # waiting.
    replan_json: Mapped[str | None] = mapped_column(Text)
    # Who the node's content is from and who let it through (revision
    # 0051): a person's id or ``agent:<slug>``; NULL where nobody is
    # recorded (a node from before the revision, one adopted from the
    # forge, a planner run that named no agent).
    proposed_by: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(Text)
    published_by: Mapped[str | None] = mapped_column(Text)
    # A reviewer's verdict on the node's level (its children), as a JSON
    # object with the digest of what was reviewed; NULL until one is given.
    review_json: Mapped[str | None] = mapped_column(Text)


class PlanEpicRunRow(Base):
    """One epic run (#2347): a person's "run this epic", which the daemon
    drives by admitting the epic's ready tasks as issue runs, in dependency
    order, each item's ``parent_item_id`` naming this row. ``state`` is
    ``running``, ``paused``, ``completed`` or ``cancelled``; the tasks are
    ``daemon_plan_epic_run_tasks``."""

    __tablename__ = "daemon_plan_epic_runs"
    __table_args__ = (Index("idx_daemon_plan_epic_runs_node", "plan_id", "node_id"),)

    epic_run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    started_by: Mapped[str | None] = mapped_column(Text)
    started_by_display: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    completed_at: Mapped[float | None] = mapped_column(REAL)


class PlanEpicRunTaskRow(Base):
    """One task of an epic run: where it stands (``waiting``, ``ready``,
    ``queued``, ``running``, ``landed``, ``closed``, ``failed`` or
    ``blocked`` by a dependency), the item it was admitted as and the run
    that item last started, so a client can link to the run's thread."""

    __tablename__ = "daemon_plan_epic_run_tasks"
    __table_args__ = (PrimaryKeyConstraint("epic_run_id", "node_id"),)

    epic_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    state: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    admitted_at: Mapped[float | None] = mapped_column(REAL)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)


class WorkMarkRow(Base):
    """What a person did to an alert, not to the work: ``dismissed`` (the
    alert is acknowledged and stops asking for attention) or ``deleted``
    (the work is hidden from every listing; its rows stay as the audit
    trail).

    A side table rather than a column on the work it marks: every write to
    ``daemon_work_items`` or ``runs`` bumps the row's ``revision``, so a
    mark kept there would refuse the next command of everyone who had read
    the row before it — and some work has no item row at all, only a run.
    ``subject_kind`` is ``item`` or ``run``; ``subject_key`` is the id as
    stored. ``cause`` says how the mark came to stand (``dismissed``,
    ``abandoned``, ``cancelled``, ``deleted``). The row is current state
    only — who did it and when is the operation ``operation_id`` names —
    and :mod:`lantern.db.work_marks` drops it when the work moves again.
    """

    __tablename__ = "daemon_work_marks"
    __table_args__ = (PrimaryKeyConstraint("subject_kind", "subject_key", "mark"),)

    subject_kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_key: Mapped[str] = mapped_column(Text, nullable=False)
    mark: Mapped[str] = mapped_column(Text, nullable=False)
    cause: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[float] = mapped_column(REAL, nullable=False)
    actor_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'{}'"))
    reason: Mapped[str | None] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(Text)


class GrantRow(Base):
    """A standing rule an owner wrote: ``agent_slug`` may take ``action``
    while ``conditions_json`` holds, at most ``daily_limit`` times a day
    (NULL is unlimited). See :mod:`lantern.daemon.controls.delegation` for
    what the conditions mean and which actions can be named at all.

    ``revision`` is bumped by every edit, in the store, and an edit names
    the revision it read. Grants ship empty: a database that has never had
    one delegates nothing.
    """

    __tablename__ = "daemon_grants"
    __table_args__ = (Index("idx_daemon_grants_subject", "agent_slug", "action"),)

    grant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_slug: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    # Only the condition keys that constrain something, as a JSON object.
    conditions_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'{}'")
    )
    daily_limit: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_by_display: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))


class GoalRow(Base):
    """A standing objective an owner wrote for one repository (revision
    0053): a title, the objective in the owner's words (``text``) and
    whether it is ``active``, ``paused`` or ``done``. The plans proposed
    from it name it in ``daemon_plans.goal_id``.

    ``revision`` is bumped by every edit, in the store, and an edit names
    the revision it read.
    """

    __tablename__ = "daemon_goals"
    __table_args__ = (Index("idx_daemon_goals_repository", "repository", "state"),)

    goal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    repository: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'active'"))
    created_by: Mapped[str | None] = mapped_column(Text)
    created_by_display: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))


class DecisionRow(Base):
    """One judged act: which agent asked to take which action, what the
    judge answered (``allow``, ``deny`` or ``escalate``) and why, the grant
    that allowed it, what it was about, and the facts it was judged on
    (``attrs_json``), kept for audit.

    A grant's daily use is counted from the ``allow`` rows here, so a
    deleted grant's rows stay: the ledger is the record, not the grant. An
    ``escalate`` row waits for a person; ``resolved_at``, ``resolved_by``
    and ``resolution`` say how it ended, and stay NULL until it does.
    """

    __tablename__ = "daemon_decisions"
    __table_args__ = (
        Index("idx_daemon_decisions_at", "at", "decision_id"),
        Index("idx_daemon_decisions_grant", "grant_id", "at"),
    )

    decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    grant_id: Mapped[str | None] = mapped_column(Text)
    agent_slug: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[str | None] = mapped_column(Text)
    node_id: Mapped[str | None] = mapped_column(Text)
    item_id: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(Text)
    epic_run_id: Mapped[str | None] = mapped_column(Text)
    repository: Mapped[str | None] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(Text)
    attrs_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'{}'"))
    at: Mapped[float] = mapped_column(REAL, nullable=False)
    resolved_at: Mapped[float | None] = mapped_column(REAL)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
