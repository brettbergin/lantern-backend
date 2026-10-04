"""The public shapes: what a client sends and what it reads back.

Every resource carries ``id``, ``workspace_id`` and RFC 3339 UTC
timestamps. Nothing here exposes a host path, a database row, or a
credential. ``available_actions`` on a read is advice for a UI; the server
rechecks eligibility when the action arrives.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lantern.daemon.controls.operations import Operation
from lantern.daemon.controls.principal import WORKSPACE_ID, Capability
from lantern.daemon.model import live_runs


def rfc3339(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Health(ApiModel):
    status: Literal["ok"] = "ok"


class Readiness(ApiModel):
    ready: bool
    generation: str | None = None
    #: Rows the public chronology is behind the engine's own; ``None`` until
    #: the projection exists.
    projection_lag: int | None = None


class Limits(ApiModel):
    page_default: int
    page_max: int
    max_body_bytes: int
    max_stream_clients: int
    auth_failures_per_minute: int
    auth_lockout_s: int


class Retention(ApiModel):
    replay_s: int
    idempotency_s: int
    operation_deadline_s: int


class Capabilities(ApiModel):
    contract_version: int = 1
    server_version: str
    workspace_id: str = WORKSPACE_ID
    features: list[str]
    run_kinds: list[str] = Field(default_factory=lambda: ["code", "workload", "tool", "plan"])
    capabilities: list[str]
    limits: Limits
    retention: Retention


class Actor(ApiModel):
    kind: str
    id: str
    display: str | None = None
    via: str


class Target(ApiModel):
    kind: str
    id: str


class Dismissal(ApiModel):
    """A person acknowledged the alert this work raises: it keeps its state
    and its controls and no longer asks anyone for attention. ``cause`` is
    ``dismissed`` for a plain acknowledgement and ``abandoned`` when the
    person gave the work up — the abandon is its own acknowledgement. Gone
    again the moment the work changes state, so a new failure is a new
    alert."""

    at: str
    by: Actor | None = None
    cause: str = "dismissed"
    reason: str | None = None
    operation_id: str | None = None


class OperationOut(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    action: str
    target: Target
    state: str
    effect: str
    actor: Actor
    request: dict[str, Any]
    accepted_at: str
    claimed_at: str | None = None
    finished_at: str | None = None
    expires_at: str | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @classmethod
    def from_operation(cls, op: Operation) -> OperationOut:
        actor = op.actor
        return cls(
            id=op.id,
            workspace_id=str(actor.get("workspace_id") or WORKSPACE_ID),
            action=op.action,
            target=Target(kind=op.target_kind, id=op.target_key),
            state=op.state,
            effect=op.effect,
            actor=Actor(
                kind=str(actor.get("kind", "operator")),
                id=str(actor.get("id", "")),
                display=actor.get("display"),
                via=str(actor.get("via", "")),
            ),
            request=dict(op.request),
            accepted_at=rfc3339(op.accepted_at) or "",
            claimed_at=rfc3339(op.claimed_at),
            finished_at=rfc3339(op.finished_at),
            expires_at=rfc3339(op.expires_at),
            result=op.result,
            error_code=op.error_code,
            error_detail=op.error_detail,
        )


class CurrentRun(ApiModel):
    item_id: str
    run_id: str
    title: str
    kind: str
    profile: str | None = None

    @classmethod
    def from_status(cls, run: Mapping[str, Any]) -> CurrentRun:
        return cls(
            item_id=str(run["item_id"]),
            run_id=str(run["run_id"]),
            title=str(run.get("title", "")),
            kind=str(run.get("kind", "code")),
            profile=run.get("profile"),
        )


class Hold(ApiModel):
    name: str
    owner: str | None = None
    via: str = ""
    reason: str = ""
    created_at: str | None = None


class RepoHealth(ApiModel):
    model_config = ConfigDict(extra="allow")

    repo: str
    state: str


class Status(ApiModel):
    """The daemon's live state, observed at ``observed_at``."""

    workspace_id: str = WORKSPACE_ID
    observed_at: str
    generation: str | None = None
    version: str
    #: The oldest run in flight.
    current: CurrentRun | None = None
    #: Every run in flight, oldest first (``current`` is the first).
    runs: list[CurrentRun] = Field(default_factory=list)
    #: How many runs the daemon executes at once (`[daemon] max_concurrent_runs`).
    max_concurrent_runs: int = 1
    claiming: str | None = None
    queued: int
    runs_today: int
    max_runs_per_day: int
    run_cap_timezone: str
    resumes_today: int = 0
    breaker_open: bool
    paused: bool
    holds: list[Hold]
    stopping: bool = False
    restarting: bool = False
    provider_hold: str | None = None
    source: str | None = None
    source_failures: int = 0
    source_retry_in_s: float = 0.0
    repos: list[RepoHealth] = Field(default_factory=list)
    #: The public chronology's high-water mark, for a client that reads a
    #: snapshot and then subscribes from it; ``None`` until it exists.
    watermark: int | None = None

    @classmethod
    def from_status(cls, status: dict[str, Any], *, now: float) -> Status:
        current = status.get("current")
        details = status.get("hold_details")
        if isinstance(details, list) and details:
            holds = [
                Hold(
                    name=str(h.get("name")),
                    owner=h.get("owner"),
                    via=str(h.get("via") or ""),
                    reason=str(h.get("reason") or ""),
                    created_at=rfc3339(h.get("created_at")),
                )
                for h in details
            ]
        else:
            holds = [Hold(name=str(name)) for name in status.get("holds") or []]
        return cls(
            observed_at=rfc3339(now) or "",
            generation=status.get("generation"),
            version=str(status.get("version", "")),
            current=CurrentRun.from_status(current) if current else None,
            runs=[CurrentRun.from_status(run) for run in live_runs(status)],
            max_concurrent_runs=int(status.get("max_concurrent_runs", 1)),
            claiming=status.get("claiming"),
            queued=int(status.get("queued", 0)),
            runs_today=int(status.get("runs_today", 0)),
            max_runs_per_day=int(status.get("max_runs_per_day", 0)),
            run_cap_timezone=str(status.get("run_cap_timezone", "UTC")),
            resumes_today=int(status.get("resumes_today", 0)),
            breaker_open=bool(status.get("breaker_open", False)),
            paused=bool(status.get("paused", False)),
            holds=holds,
            stopping=bool(status.get("stopping", False)),
            restarting=bool(status.get("restarting", False)),
            provider_hold=status.get("provider_hold"),
            source=status.get("source"),
            source_failures=int(status.get("source_failures", 0)),
            source_retry_in_s=float(status.get("source_retry_in_s", 0.0)),
            repos=[
                RepoHealth.model_validate(r)
                for r in status.get("repos") or []
                if isinstance(r, dict) and "repo" in r and "state" in r
            ],
        )


# -- auth -------------------------------------------------------------------------


class TokenRequest(ApiModel):
    grant_type: Literal["client_credentials", "refresh_token"]
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None


class TokenResponse(ApiModel):
    token_type: Literal["Bearer"] = "Bearer"
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    scope: str
    client_id: str


class OidcProviderOut(ApiModel):
    """What a signed-out browser needs to start Authorization Code + PKCE."""

    id: str
    label: str
    authorize_url: str
    client_id: str
    scopes: list[str]
    end_session_url: str | None
    #: Private-use scheme redirects (RFC 8252 section 7.1) a native app may
    #: present beside the web redirect; empty when none is configured.
    native_redirect_uris: list[str] = Field(default_factory=list)


class AuthProviders(ApiModel):
    #: Server-enforced local-auth policy, bounded OIDC sessions and signed logout.
    policy_version: int = 1
    oidc_session_max_age_s: int | None = None
    #: Username and password sign-in (``/v1/auth/local/login``) is offered.
    local: bool
    #: The OpenID Connect provider, when one is configured and reachable.
    oidc: OidcProviderOut | None
    #: The name the product agent (``concierge``) answers to, so a
    #: signed-out client can say who it is signing in to.
    assistant_name: str


class OidcTokenRequest(ApiModel):
    """The browser's authorization code, redeemed by the daemon."""

    provider: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=4096)
    #: RFC 7636: 43 to 128 unreserved characters.
    code_verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    redirect_uri: str = Field(min_length=1, max_length=2048)
    nonce: str = Field(min_length=1, max_length=512)


class RevokeRequest(ApiModel):
    #: The refresh token whose family to revoke, besides the access token
    #: this request was made with.
    refresh_token: str | None = None


class Me(ApiModel):
    client_id: str
    name: str
    capabilities: list[Capability]
    workspace_id: str = WORKSPACE_ID
    token_expires_at: str


# -- work items, runs, the queue (#1036) ------------------------------------------

OriginKind = Literal["issue", "chat", "schedule", "api", "other"]


class Origin(ApiModel):
    """Where a work item came from. ``ref`` is the id the daemon's own
    surfaces (ctl, chat, the console) know the item by."""

    kind: OriginKind
    repository_id: str | None = None
    repository: str | None = None
    number: int | None = None
    url: str | None = None
    ref: str


class Item(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    kind: str
    state: str
    title: str
    origin: Origin
    profile: str | None = None
    recipe: str | None = None
    recipe_target: str | None = None
    attempts: int = 0
    run_id: str | None = None
    last_error: str | None = None
    pending_report: str | None = None
    not_before: str | None = None
    created_at: str
    updated_at: str
    revision: int = 0
    available_actions: list[str] = Field(default_factory=list)
    #: Set while a person's dismissal of this item's alert stands.
    dismissal: Dismissal | None = None
    #: When a person deleted the item: it is left out of every listing and
    #: takes no further command; the record stays readable by its id.
    deleted_at: str | None = None
    #: The agent asked to lead the work, or, once the run is planned, the
    #: agent that leads it. ``None`` for work admitted without one.
    lead_agent: str | None = None
    #: The agent in each run role: the planned team once the item was
    #: dispatched, the roles asked for before. ``None`` when none were.
    assignment: dict[str, str] | None = None
    #: A conversation the requesting viewer can open, when one exists.
    #: This is a presentation link, not the item's execution admission.
    channel_id: str | None = None


class ItemDetail(Item):
    body: str = ""
    #: Every run the item was dispatched under, oldest first.
    runs: list[str] = Field(default_factory=list)
    #: Who admitted the item through a recorded operation, when one did.
    admitted_by: Actor | None = None


class PullRequest(ApiModel):
    number: int | None = None
    url: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    title: str | None = None


class Rounds(ApiModel):
    review: int = 0
    ci: int = 0
    granted: int = 0
    #: Which budget the run ran out of (``review`` / ``ci``), or ``None``.
    exhausted: str | None = None


class PublishedOut(ApiModel):
    sink: str
    location: str
    tasks: list[str] = Field(default_factory=list)
    files: int = 0


class GateSummary(ApiModel):
    kind: str
    state: str
    revision: int = 0


class Run(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    kind: str
    state: str
    stage: str | None = None
    outcome: str
    reason: str | None = None
    item_id: str | None = None
    created_at: str
    updated_at: str
    pull_request: PullRequest | None = None
    rounds: Rounds = Field(default_factory=Rounds)
    published: list[PublishedOut] = Field(default_factory=list)
    gate: GateSummary | None = None
    review_wait: str | None = None
    revision: int = 0
    available_actions: list[str] = Field(default_factory=list)
    #: Set while a dismissal of this run's alert stands: its work item's
    #: when one pins the run, the run's own otherwise.
    dismissal: Dismissal | None = None
    #: When a person deleted the run's work: left out of every listing,
    #: its run directory removed; the record stays readable by its id.
    deleted_at: str | None = None


class TaskOutputOut(ApiModel):
    summary: str = ""
    files: list[str] = Field(default_factory=list)


class Task(ApiModel):
    id: str
    title: str
    description: str = ""
    state: str
    depends_on: list[str] = Field(default_factory=list)
    revisions: int = 0
    replans: int = 0
    verify_suspect: bool = False
    verify_reauthors: int = 0
    output: TaskOutputOut | None = None


class QueueEntry(ApiModel):
    position: int
    item: Item
    #: When dispatch's own rule lets the item go; ``None`` means now.
    eligible_at: str | None = None
    eligible: bool = True
    reason: str | None = None


class QueuePage(ApiModel):
    """The queue in dispatch order, with what stands in its way."""

    data: list[QueueEntry]
    next_cursor: str | None = None
    has_more: bool = False
    observed_at: str
    paused: bool = False
    breaker_open: bool = False


class RepositoryLabel(ApiModel):
    """One label the loop applies to ``repository``'s issues: the name it
    carries here (a ``[[vcs.repos]]`` rename included), what it is for,
    and whether the repository has it."""

    name: str
    #: ``trigger``, ``in_progress``, ``failed``, ``completed``, ``blocked``,
    #: ``gated``, ``workload`` — or ``followup``, the label put on the
    #: issues a merged run files.
    kind: str
    description: str
    #: Six hex digits, no ``#``: the color a label this daemon creates gets.
    color: str
    #: Null while the repository's labels are unknown: nobody has been
    #: able to look, so nothing is claimed about this one either.
    present: bool | None = None


class RepositoryLabels(ApiModel):
    """Whether a repository carries the labels lantern relies on.

    ``compliant``: it carries every one of them, as of ``checked_at``.
    ``incomplete``: ``missing`` names the ones it does not carry — a
    label sync creates exactly those. ``unknown``: nobody has been able
    to look yet (no reading has been taken, the forge would not answer,
    or the configured names have changed since the last reading), which
    is never reported as compliant.
    """

    state: Literal["compliant", "incomplete", "unknown"]
    #: Every label name the loop applies to this repository.
    expected: list[str] = Field(default_factory=list)
    #: The ones it does not carry; empty while ``state`` is not ``incomplete``.
    missing: list[str] = Field(default_factory=list)
    labels: list[RepositoryLabel] = Field(default_factory=list)
    #: When the labels were last read from the forge; null while unknown.
    checked_at: str | None = None


class RepositoryPlanning(ApiModel):
    """What a plan looks like on this repository's forge (#2340):
    ``native`` sub-issues, a managed ``checklist`` in the parent, or
    ``unsupported`` with the reason a person reads."""

    hierarchy: Literal["native", "checklist", "unsupported"]
    reason: str | None = None


class Repository(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    repository: str
    forge: str
    enabled: bool = True
    deliver_base: str | None = None
    trigger_label: str
    workload_label: str
    health: RepoHealth | None = None
    #: Where the registration came from (``config``: imported from
    #: lantern.toml; ``api``), who made it and when.
    source: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    #: The registration's enabled state differs from what this daemon
    #: process polls: polling follows at the next start.
    restart_required: bool = False
    #: Whether the repository carries the labels the loop applies, as of
    #: the daemon's last reading (``[daemon] label_check_interval_s``) or
    #: the last label sync.
    labels: RepositoryLabels
    planning: RepositoryPlanning


class RepositoryCreate(ApiModel):
    """A registration: ``owner/name`` (``group/subgroup/project`` on
    GitLab), the forge it lives on (``[vcs] kind`` when unset), whether it
    is polled and run, and the branch it delivers to (the repository's
    default when unset)."""

    repository: str = Field(min_length=3, max_length=200)
    forge: Literal["github", "gitlab", "gitea"] | None = None
    enabled: bool = True
    deliver_base: str | None = Field(default=None, max_length=200)


class RepositoryUpdate(ApiModel):
    """A change to a registration: only the fields sent change;
    ``deliver_base: null`` clears it."""

    enabled: bool | None = None
    deliver_base: str | None = Field(default=None, max_length=200)


class DiscoveryCredential(ApiModel):
    """The credential a discovery listed with: a personal token (``pat``,
    with the account it belongs to) or a GitHub App installation (``app``;
    the installation, not a person, so no login)."""

    mode: Literal["pat", "app"]
    login: str | None = None


class AvailableRepository(ApiModel):
    """A repository the host's forge credential can see: what a
    person picks from when registering one. ``configured`` says whether
    it is declared to this daemon already."""

    repository: str
    forge: str
    owner: str
    name: str
    private: bool = False
    archived: bool = False
    default_branch: str | None = None
    url: str | None = None
    configured: bool = False


class RepositoryDiscovery(ApiModel):
    workspace_id: str = WORKSPACE_ID
    forge: str
    credential: DiscoveryCredential
    data: list[AvailableRepository]
    #: True when the forge holds more than the listing walked; the rest is
    #: not in ``data``.
    truncated: bool = False


class OpenIssue(ApiModel):
    number: int
    title: str


class OpenIssuePage(ApiModel):
    data: list[OpenIssue]
    has_more: bool


class Profile(ApiModel):
    id: str
    name: str
    description: str = ""
    sinks: list[str] = Field(default_factory=list)
    publish: str = "auto"
    repo: bool = False
    default: bool = False


class Recipe(ApiModel):
    id: str
    name: str
    parameters: list[str] = Field(default_factory=list)
    enabled: bool = True


# -- intake and item commands ------------------------------------------------------


class IssueIntake(ApiModel):
    kind: Literal["issue"]
    #: The repository by its public id or its ``owner/name``; one of the two.
    repository_id: str | None = None
    repository: str | None = None
    number: int = Field(ge=1)
    run_kind: Literal["code", "workload"] = "code"
    lead: str | None = Field(default=None, max_length=64)
    roles: dict[str, str] = Field(default_factory=dict, max_length=8)
    channel_id: str | None = Field(default=None, max_length=128)


class WorkloadIntake(ApiModel):
    kind: Literal["workload"]
    ask: str = Field(min_length=1, max_length=65536)
    profile: str | None = None
    sink: str | None = None
    #: The agent that leads the run (the built-in lead when omitted); it
    #: must be active and declare the ``lead`` role.
    lead: str | None = Field(default=None, max_length=64)
    #: The agent asked for in each run role (``planner``, ``builder``,
    #: ``critic``, ``operator``); each must be active and declare the role.
    roles: dict[str, str] = Field(default_factory=dict, max_length=8)
    #: The channel the work answers to: its result is delivered there.
    channel_id: str | None = Field(default=None, max_length=128)


class ToolIntake(ApiModel):
    kind: Literal["tool"]
    recipe: str = Field(min_length=1, max_length=64)
    parameters: dict[str, str] = Field(default_factory=dict)


IntakeRequest = Annotated[IssueIntake | WorkloadIntake | ToolIntake, Field(discriminator="kind")]


class Admitted(ApiModel):
    item: Item
    operation: OperationOut
    #: ``False`` when the same request (or a poll) had already queued it.
    created: bool


class ItemCommand(ApiModel):
    reason: str | None = Field(default=None, max_length=2000)
    expected_revision: int | None = Field(default=None, ge=0)


class WorkDeleteCommand(ApiModel):
    reason: str | None = Field(default=None, max_length=2000)
    expected_revision: int | None = Field(default=None, ge=0)
    #: Delete even when a run's workspace is the only copy of work that
    #: was never delivered. Off by default: that work would be lost.
    discard_undelivered: bool = False


class ItemCommandResult(ApiModel):
    item: Item
    operation: OperationOut


# -- the public chronology (#1037) -------------------------------------------------


class EventOut(ApiModel):
    """One public event. ``id`` is the replay cursor (``evt_<seq>``);
    ``native_seq`` is the engine's own sequence when the event is a
    projection of the run's chronology; ``actor`` is absent where nothing
    truthful can be said."""

    id: str
    schema_version: int = 1
    type: str
    occurred_at: str
    recorded_at: str
    workspace_id: str = WORKSPACE_ID
    run_id: str | None = None
    item_id: str | None = None
    operation_id: str | None = None
    actor: Actor | None = None
    causation_id: str | None = None
    native_seq: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# -- steering and gates (#1038) ----------------------------------------------------


class SteerRequest(ApiModel):
    text: str = Field(min_length=1, max_length=16384)
    #: What the instruction cites — message ids, URLs, revisions the
    #: person read — kept on the record, never interpreted.
    source_refs: list[str] = Field(default_factory=list, max_length=32)
    expected_revision: int | None = Field(default=None, ge=0)
    #: The task lane the instruction is for, so it is answered by that task
    #: rather than by whichever lane reaches a boundary first, and the agent
    #: that was mentioned, so the answer comes back in its persona (S-A11).
    #: Both optional: naming neither steers the run as it always did, and a
    #: target the run does not have falls back to the same.
    task_id: str | None = Field(default=None, max_length=128)
    agent_slug: str | None = Field(default=None, max_length=64)


class Steering(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    run_id: str
    status: str
    text: str
    source_refs: list[str] = Field(default_factory=list)
    actor: Actor
    expected_revision: int | None = None
    submitted_at: str
    deadline_at: str | None = None
    delivered_at: str | None = None
    handled_at: str | None = None
    reply: str | None = None
    action: str | None = None
    error: str | None = None
    operation_id: str | None = None


class SteerResult(ApiModel):
    steering: Steering
    operation: OperationOut


class RunCommand(ApiModel):
    reason: str | None = Field(default=None, max_length=2000)
    retry: bool = False
    expected_revision: int | None = Field(default=None, ge=0)


class RoundGrant(ApiModel):
    rounds: int = Field(ge=1, le=100)
    expected_revision: int | None = Field(default=None, ge=0)


class RunCommandResult(ApiModel):
    run: Run
    operation: OperationOut
    message: str | None = None


class Gate(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    kind: str
    state: str
    run_id: str
    item_id: str | None = None
    repository: str | None = None
    pull_request: PullRequest | None = None
    #: The head the gate's pull request stood at when last observed — what
    #: an approval is judged against, named so the client can compare.
    head_sha: str | None = None
    created_at: str
    resolved_at: str | None = None
    resolved_by: str | None = None
    detail: str | None = None
    revision: int = 0
    required_capability: str = "gates:approve"
    available_actions: list[str] = Field(default_factory=list)
    #: Set while a dismissal of the gated item's alert stands.
    dismissal: Dismissal | None = None


class GateApproval(ApiModel):
    expected_revision: int = Field(ge=0)


#: The most alerts one bulk dismissal may name: a page of them.
ATTENTION_DISMISS_MAX = 200


class AttentionTarget(ApiModel):
    """One alert a bulk dismissal names: an item, or a run no item
    carries. ``expected_revision`` pins the state the person was shown."""

    item_id: str | None = None
    run_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _one_subject(self) -> AttentionTarget:
        if (self.item_id is None) == (self.run_id is None):
            raise ValueError("name item_id or run_id, not both")
        return self


class AttentionDismissRequest(ApiModel):
    targets: list[AttentionTarget] = Field(min_length=1, max_length=ATTENTION_DISMISS_MAX)
    reason: str | None = Field(default=None, max_length=2000)


class AttentionDismissResult(ApiModel):
    """What became of one named alert. ``skipped`` carries the refusal the
    same dismissal would have had on its own route."""

    item_id: str | None = None
    run_id: str | None = None
    outcome: Literal["dismissed", "already_dismissed", "skipped"]
    code: str | None = None
    detail: str | None = None


class AttentionDismissed(ApiModel):
    operation: OperationOut
    #: One entry per target, in the order the request named them.
    results: list[AttentionDismissResult]


AttentionGroup = Literal["decision", "failed", "paused"]


class AttentionAction(ApiModel):
    """One action the server offers on an entry right now, the capability
    it needs, and whether the caller holds that capability."""

    action: str
    capability: str
    allowed: bool


class AttentionEntry(ApiModel):
    """One thing waiting on a person. ``id`` is ``<kind>:<natural key>``,
    opaque and stable while the same thing waits; the reference fields say
    what it is about. ``kind`` is open: a later release adds kinds, and a
    client leaves out an entry whose kind it does not know."""

    id: str
    workspace_id: str = WORKSPACE_ID
    kind: str
    group: AttentionGroup
    #: The state word of what waits, in its own resource's vocabulary.
    state: str
    title: str
    reason: str | None = None
    #: When it started waiting; ``null`` when nothing recorded it.
    since: str | None = None
    repository: str | None = None
    repository_id: str | None = None
    item_id: str | None = None
    run_id: str | None = None
    gate_id: str | None = None
    plan_id: str | None = None
    node_id: str | None = None
    epic_run_id: str | None = None
    #: Set only when the caller can read the conversation.
    channel_id: str | None = None
    #: The revision of the gate (a ``gate`` entry) or the item (an ``item``
    #: entry); ``null`` where the thing waiting has none.
    revision: int | None = None
    actions: list[AttentionAction] = Field(default_factory=list)
    #: Set only on an entry listed with ``include_dismissed``.
    dismissal: Dismissal | None = None


class AttentionCounts(ApiModel):
    """How much is waiting, in every group, whatever the page shows."""

    total: int = 0
    decision: int = 0
    failed: int = 0
    paused: int = 0


class AttentionPage(ApiModel):
    data: list[AttentionEntry]
    next_cursor: str | None = None
    has_more: bool = False
    counts: AttentionCounts
    observed_at: str


class GateResult(ApiModel):
    gate: Gate
    operation: OperationOut
    message: str | None = None


# -- artifacts and usage (#1039) ---------------------------------------------------


class ArtifactOut(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    run_id: str
    task_id: str | None = None
    #: The file's path inside the run's artifact tree — never a host path.
    path: str
    size: int
    sha256: str
    media_type: str
    #: ``workspace`` (a mounted code run's tree), ``harvest`` (copied out of
    #: an unmounted one), ``sink`` (what a workload or tool run declared).
    origin: str
    recorded_at: str
    available: bool
    tombstoned_at: str | None = None


class ArtifactPage(ApiModel):
    """A run's catalog, and — separately — where the run published: a
    catalog entry is a file on the host, a ``published`` entry a sink
    that took it."""

    data: list[ArtifactOut]
    next_cursor: str | None = None
    has_more: bool = False
    published: list[PublishedOut] = Field(default_factory=list)
    #: Files the catalog left out past its cap; the host listing has them.
    beyond_cap: int = 0


class UsageTotals(ApiModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


class UsageAgent(ApiModel):
    agent: str
    usage: UsageTotals
    turns: int = 0
    jobs: int = 0


class RunUsage(ApiModel):
    run_id: str
    workspace_id: str = WORKSPACE_ID
    #: Whether the backend reported anything at all; ``False`` is not zero.
    recorded: bool
    turns: int = 0
    models: list[str] = Field(default_factory=list)
    total: UsageTotals
    by_agent: list[UsageAgent] = Field(default_factory=list)
    by_phase_model: dict[str, UsageTotals] = Field(default_factory=dict)
    #: Never a number: no backend reports a charge in a known unit.
    spend: None = None
    spend_basis: str


class UsageWindowRun(ApiModel):
    run_id: str
    kind: str
    state: str
    recorded: bool
    turns: int = 0
    total: UsageTotals


class UsageWindow(ApiModel):
    workspace_id: str = WORKSPACE_ID
    since: str
    until: str
    observed_at: str
    runs: list[UsageWindowRun] = Field(default_factory=list)
    runs_considered: int = 0
    runs_recorded: int = 0
    turns: int = 0
    models: list[str] = Field(default_factory=list)
    total: UsageTotals
    spend: None = None
    spend_basis: str


class UsagePool(ApiModel):
    """Today's workspace budget pool: runs against the daily cap and
    reported tokens (input plus output) against the daily budget, for the
    calendar day in ``[daemon] run_cap_timezone``."""

    workspace_id: str = WORKSPACE_ID
    day_start: str
    resets_at: str
    runs_today: int
    max_runs_per_day: int
    tokens_today: int
    #: ``null`` when no budget is configured.
    daily_token_budget: int | None = None
    runs_tokens_today: int
    turns_tokens_today: int


# -- fleet analytics ----------------------------------------------------------------


class AnalyticsLane(ApiModel):
    """One run kind's totals over a window — or every kind together
    (``all``), or the window before this one (``previous``)."""

    kind: str
    runs: int
    #: Runs that finished the way they were meant to: merged or completed.
    landed: int
    failed: int
    #: A person's decision, not an outcome: counted, never judged.
    cancelled: int
    turns: int
    #: Input plus output tokens the backends reported.
    tokens: int
    cache_read_tokens: int
    #: Seconds the runs' phase attempts were actually running.
    active_s: float
    #: Seconds from each run's creation to its last update.
    elapsed_s: float
    #: Elapsed time the loop did not spend working: waiting on a person.
    parked_s: float
    #: Landed over landed plus failed; ``null`` when no run was judged.
    ok_rate: float | None
    parked_share: float


class AnalyticsPhase(ApiModel):
    """One phase's share of the window, by the attempts that started in it."""

    phase: str
    attempts: int
    #: Attempts past the first: where the loop went round again.
    retries: int
    turns: int
    tokens: int
    cache_read_tokens: int
    active_s: float


class AnalyticsBucket(ApiModel):
    """One slice of the window, by the runs that began in it."""

    since: str
    until: str
    #: Every run that began here, whatever its state now.
    runs: int
    landed: int
    failed: int
    cancelled: int
    turns: int


class AnalyticsRework(ApiModel):
    tasks: int
    revisions: int
    replans: int
    #: Tasks flagged as having a suspect verify.
    suspect: int
    retried_share: float


class AnalyticsFailure(ApiModel):
    #: The head of the failed runs' reason: the class, not one run's detail.
    reason: str
    count: int


class AnalyticsRun(ApiModel):
    run_id: str
    kind: str
    state: str
    turns: int
    tokens: int
    active_s: float
    parked_s: float


class AnalyticsSpread(ApiModel):
    median: float
    p90: float


class AnalyticsSpreads(ApiModel):
    """Median and p90 across the window's runs; ``null`` where no run
    gives one."""

    turns: AnalyticsSpread | None
    #: Creation to last update, over the runs that landed: time to land.
    cycle_s: AnalyticsSpread | None
    active_s: AnalyticsSpread | None


class AnalyticsDelta(ApiModel):
    """This window's total against the previous window's, as a share of
    the previous value (``0.25`` is a quarter more). ``null`` when nothing
    preceded the window or the previous value was zero."""

    runs: float | None
    landed: float | None
    failed: float | None
    cancelled: float | None
    turns: float | None
    tokens: float | None
    cache_read_tokens: float | None
    active_s: float | None
    elapsed_s: float | None
    parked_s: float | None
    ok_rate: float | None
    parked_share: float | None


class AnalyticsWindow(ApiModel):
    """A window of runs, folded: outcomes, time, turns and failures by
    cause. A run is attributed whole to the window it began in. Telemetry,
    never a currency."""

    workspace_id: str = WORKSPACE_ID
    since: str
    until: str
    observed_at: str
    window_s: int
    #: No run began in the window.
    empty: bool
    total: AnalyticsLane
    lanes: list[AnalyticsLane]
    phases: list[AnalyticsPhase]
    buckets: list[AnalyticsBucket]
    rework: AnalyticsRework
    review_rounds: int
    ci_rounds: int
    failures: list[AnalyticsFailure]
    costliest: list[AnalyticsRun]
    longest_parked: list[AnalyticsRun]
    spreads: AnalyticsSpreads
    #: The window before this one, every kind together; ``null`` when no
    #: run began in it.
    previous: AnalyticsLane | None
    delta: AnalyticsDelta


# -- the briefing -------------------------------------------------------------------
#
# One small summary for a landing screen, a widget and a digest. Every
# field is always present; what cannot be said is ``null``. Each part is
# its own object so a later release adds a field inside it — clients
# ignore fields they do not know.


class BriefingLane(ApiModel):
    """How the runs of one kind ended inside the window."""

    kind: str
    landed: int = 0
    failed: int = 0
    cancelled: int = 0


class BriefingLandedRun(ApiModel):
    """One run that landed in the window: the work it did and where."""

    run_id: str
    kind: str
    #: The work item's title; the run's own ask when no item carries it.
    title: str
    repository: str | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    landed_at: str


class BriefingOutcomes(ApiModel):
    """The runs that reached an end inside the window — by when they
    finished, not when they began. ``blocked`` is not an end: it waits."""

    #: Merged or completed.
    landed: int = 0
    failed: int = 0
    #: A person's decision, not an outcome.
    cancelled: int = 0
    by_kind: list[BriefingLane]
    #: Newest first, at most ten; deleted runs are left out.
    recent_landed: list[BriefingLandedRun]


class BriefingWaiting(ApiModel):
    """The attention list's counts, and when its longest wait began."""

    total: int = 0
    decision: int = 0
    failed: int = 0
    paused: int = 0
    oldest_since: str | None = None


class BriefingDecision(ApiModel):
    """One act an agent was allowed to take, with what it was about."""

    id: str
    grant_id: str | None = None
    agent_slug: str
    action: str
    reason: str
    at: str
    plan_id: str | None = None
    node_id: str | None = None
    item_id: str | None = None
    run_id: str | None = None
    epic_run_id: str | None = None
    repository: str | None = None
    operation_id: str | None = None


class BriefingDecided(ApiModel):
    """What agents decided under grants inside the window, and the
    escalations still waiting for a person, however old."""

    allow: int = 0
    deny: int = 0
    escalate: int = 0
    unresolved_escalations: int = 0
    #: The allowed acts, newest first, at most ten — for a caller holding
    #: ``audit:read``; ``null`` for anyone else, who reads the counts.
    recent: list[BriefingDecision] | None = None


class BriefingSupply(ApiModel):
    """How much work is lined up, as it stands now."""

    #: Plan nodes awaiting a person's approval.
    proposed: int = 0
    #: Plan nodes approved and not yet published.
    approved: int = 0
    #: Published tasks on the forge, open, that no epic run has started.
    ready_tasks: int = 0
    #: The daemon queue's depth.
    queued: int = 0
    #: Runs in flight.
    running: int = 0
    #: Work parked on a person: the attention list's decisions and pauses.
    parked: int = 0


class BriefingRunway(ApiModel):
    """How long the tasks lined up would take at the trailing week's rate
    of landed ``code`` runs. Both rates are ``null`` when nothing landed in
    that week: no rate is invented."""

    ready_tasks: int = 0
    landed_per_day: float | None = None
    days: float | None = None


class BriefingBudget(ApiModel):
    """Today against the daily cap and the token budget, as the usage pool
    counts them."""

    runs_today: int
    max_runs_per_day: int
    tokens_today: int
    #: ``null`` when no budget is configured.
    daily_token_budget: int | None = None
    resets_at: str


class BriefingGrants(ApiModel):
    """The grants in force, and how many have spent today's limit."""

    enabled: int = 0
    at_limit: int = 0


class Briefing(ApiModel):
    """What happened in a window, what needs a person now, and what is
    lined up. Durations are seconds and timestamps RFC 3339; nothing is a
    currency."""

    workspace_id: str = WORKSPACE_ID
    since: str
    until: str
    observed_at: str
    outcomes: BriefingOutcomes
    waiting: BriefingWaiting
    decided: BriefingDecided
    supply: BriefingSupply
    runway: BriefingRunway
    budget: BriefingBudget
    grants: BriefingGrants


# -- diagnostics and administration (#1040) -----------------------------------------


class LogRecord(ApiModel):
    timestamp: str
    level: str
    logger: str
    message: str


class LogTail(ApiModel):
    """The daemon's most recent log records, oldest first, redacted."""

    records: list[LogRecord]
    #: Lines the ring buffer holds in all; ``tail`` is what was asked for.
    buffer_size: int
    tail: int
    level: str | None = None
    grep: str | None = None
    observed_at: str


class ConfigurationEntry(ApiModel):
    key: str
    value: Any = None
    #: The layer that answers for the key now (``home config``, ``env``,
    #: ``default``…); ``None`` when the loader could not say.
    source: str | None = None
    #: ``live`` when a change applies before the next agent phase;
    #: ``restart`` when the daemon reads it at start.
    applies: Literal["live", "restart"]
    #: The file resolves to a different value than the daemon runs on: an
    #: edit that waits for a restart.
    pending: bool = False
    #: Why the daemon's own tools may not change it, when they may not.
    locked: str | None = None
    doc: str | None = None


class Configuration(ApiModel):
    """The allowlisted effective configuration: never a secret value,
    never a host path; ``sections`` says what is readable at all."""

    workspace_id: str = WORKSPACE_ID
    observed_at: str
    sections: list[str]
    entries: list[ConfigurationEntry]


class HoldRequest(ApiModel):
    name: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=500)


class HoldResult(ApiModel):
    hold: str | None = None
    #: Every hold standing after the change.
    holds: list[Hold]
    created: bool = False
    operation: OperationOut


class DaemonCommandResult(ApiModel):
    """A stop or restart accepted: the effect follows the reply, so a
    client that sees this body has a durable record, not a process exit."""

    accepted: Literal[True] = True
    action: Literal["stop", "restart"]
    #: The generation that accepted the request; a restart shows a new one
    #: on ``/health/ready`` once the daemon is back and ready.
    generation: str | None = None
    supervisor: str | None = None
    now: bool = False
    #: The run that finishes (or, ``now``, is cancelled) before the exit.
    current: CurrentRun | None = None
    operation: OperationOut


class RestartRequest(ApiModel):
    now: bool = False


class RepositoryResult(ApiModel):
    #: The repository as it stands after the command; None once removed.
    repository: Repository | None = None
    message: str = ""
    operation: OperationOut


class RepositoryLabelSync(ApiModel):
    """A label sync: the repository as it stands after it, what the sync
    created, and — when the forge refused a creation — what is still
    missing."""

    repository: Repository | None = None
    labels: RepositoryLabels
    #: The labels this sync created; empty when the repository already
    #: carried every one of them.
    created: list[str] = Field(default_factory=list)
    message: str = ""
    operation: OperationOut


class Schedule(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    name: str
    cadence: str
    timezone: str
    profile: str
    ask: str
    source: str
    created_by: str | None = None
    created_at: str | None = None
    last_due: str | None = None
    last_fired_at: str | None = None
    last_item: str | None = None
    next_due: str | None = None
    paused: bool = False
    paused_by: str | None = None
    available_actions: list[str] = Field(default_factory=list)


class ScheduleCreate(ApiModel):
    name: str
    profile: str
    ask: str
    every: str | None = None
    cron: str | None = None
    timezone: str | None = None


class ScheduleUpdate(ScheduleCreate):
    """Complete replacement for one schedule, applied atomically."""


class ScheduleResult(ApiModel):
    schedule: Schedule | None = None
    message: str
    operation: OperationOut


class GrantConditions(ApiModel):
    """What must hold for a grant to cover an act. A key left out (or
    ``null``, or ``false`` for ``require_review``) constrains nothing; which
    keys an action accepts is checked when the grant is written."""

    repositories: list[str] | None = None
    levels: list[str] | None = None
    max_children: int | None = Field(default=None, ge=1)
    require_review: bool = False
    causes: list[str] | None = None
    max_retries: int | None = Field(default=None, ge=1)


class GrantOut(ApiModel):
    """A standing rule: ``agent_slug`` may take ``action`` while
    ``conditions`` hold, at most ``daily_limit`` times a day (``null`` is
    unlimited). ``used_today`` is how many acts it allowed in the current
    cap day, counted from the decisions ledger."""

    id: str
    workspace_id: str = WORKSPACE_ID
    agent_slug: str
    action: str
    conditions: GrantConditions
    daily_limit: int | None = None
    used_today: int = 0
    enabled: bool = True
    note: str | None = None
    created_by: str | None = None
    created_by_display: str | None = None
    created_at: str
    updated_at: str
    revision: int


class GrantCreate(ApiModel):
    agent_slug: str
    action: str
    conditions: GrantConditions = Field(default_factory=GrantConditions)
    daily_limit: int | None = Field(default=None, ge=1)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=500)


class GrantUpdate(ApiModel):
    """An edit against the revision the client read. Only the fields sent
    change: ``daily_limit: null`` lifts the limit, ``note: null`` clears the
    note, and ``conditions`` replaces the whole set. A grant's agent and
    action are not edited."""

    expected_revision: int
    conditions: GrantConditions | None = None
    daily_limit: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=500)


class GrantResult(ApiModel):
    grant: GrantOut | None = None
    message: str
    operation: OperationOut


class DecisionOut(ApiModel):
    """One judged act from the ledger: who asked to take what, the outcome
    (``allow``, ``deny`` or ``escalate``) and why, the grant that allowed it,
    what it was about, and the facts it was judged on. An escalation carries
    how it was resolved once it is."""

    id: str
    workspace_id: str = WORKSPACE_ID
    grant_id: str | None = None
    agent_slug: str
    action: str
    outcome: str
    reason: str
    plan_id: str | None = None
    node_id: str | None = None
    item_id: str | None = None
    run_id: str | None = None
    epic_run_id: str | None = None
    repository: str | None = None
    operation_id: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
    at: str
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolution: str | None = None
