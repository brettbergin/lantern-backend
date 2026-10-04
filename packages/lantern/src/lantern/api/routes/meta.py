"""What this installation offers: the contract version, features, limits."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lantern import __version__
from lantern.api.auth.deps import Authenticated, current, get_ctx
from lantern.api.auth.ratelimit import FailureLimiter
from lantern.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from lantern.api.models import Capabilities, Limits, Me, Retention, rfc3339
from lantern.config import Config
from lantern.daemon.controls.principal import CAPABILITIES
from lantern.plans.hierarchy import planning_available

router = APIRouter(prefix="/v1", tags=["meta"])

#: What this release serves; a later stage appends to it.
FEATURES: tuple[str, ...] = (
    "status",
    "status.runs",
    "operations",
    "auth.client_credentials",
    "auth.refresh",
    "auth.sso_policy",
    "items",
    "queue",
    "runs",
    "intake.issue",
    "intake.workload",
    "intake.tool",
    "intake.assignment",
    "events",
    "events.stream",
    "ws",
    "runs.control",
    "steering",
    "gates",
    "artifacts",
    "usage",
    "usage.pool",
    "briefing",
    "diagnostics.logs",
    "diagnostics.configuration",
    "daemon.holds",
    "daemon.lifecycle",
    "repositories.resume",
    "repositories.issues",
    "schedules",
    "auth.local_user",
    "collaboration.channels",
    "collaboration.turns",
    "collaboration.agents",
    "collaboration.teams",
    "collaboration.preferences",
    "collaboration.workflows",
    "collaboration.connections.read",
    "collaboration.connections.manage",
    "collaboration.message_artifacts",
    "collaboration.channel_artifacts",
    "collaboration.file_inputs_generic",
    "collaboration.message_authors",
    "agents.registry",
    "agents.memory",
    "users.directory",
    "workspace.members",
    "collaboration.participants",
    "collaboration.channel_members",
    "events.scoped",
    "collaboration.bridges",
    "agents.initiative",
    "collaboration.lead_orchestrator",
    "collaboration.run_progress",
    "collaboration.channel_stop",
    "collaboration.silence",
    "collaboration.read_state",
    "collaboration.mention_steering",
    "collaboration.external_work",
    "collaboration.channel_runs",
    "repositories.discover",
    "repositories.manage",
    "repositories.labels",
    "work.dismiss",
    "work.dismiss_all",
    "work.delete",
    "analytics",
    "attention",
    "delegation",
    # The operator agent retries failures and grants rounds under grants.
    "delegation.triage",
    "attention.act",
    "attention.decisions",
)


def features(config: Config) -> list[str]:
    """What this daemon serves as configured: the release's features plus
    the ones an operator switches on."""
    served = list(FEATURES)
    if config.api.oidc.enabled:
        served.append("auth.oidc")
        served.append("auth.oidc.backchannel_logout")
        if config.api.oidc.native_redirect_uris:
            served.append("auth.oidc.native")
    if config.push.available:
        served.append("push.apns_relay")
    if planning_available(config):
        served.append("planning")
        # The clarifying step and its answers route ride every plan run
        # (#2345); `[planning] max_questions = 0` only means none are asked.
        served.append("planning.clarify")
        served.append("planning.generated_root")
        # Epic runs admit a published epic's tasks as issue runs (#2347).
        served.append("planning.run")
        # A plan carries `advance` and each node who proposed, approved and
        # published it and its level's review; `advance` is set under
        # `plans:publish`.
        served.append("planning.advance")
        # ... and an `auto` plan is moved forward by the daemon's plan
        # driver under the owner's grants: approved, published and run as
        # an agent, every judgement on the decisions ledger.
        served.append("planning.driver")
        # Goals (`/v1/goals`): the standing objectives an owner sets for a
        # repository that can hold a plan, and the plans proposed from each.
        served.append("goals")
    return served


@router.get("/capabilities", response_model=Capabilities)
async def capabilities(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(current),  # noqa: B008
) -> Capabilities:
    api = ctx.api
    limiter = FailureLimiter()
    return Capabilities(
        server_version=__version__,
        features=features(ctx.config),
        capabilities=list(CAPABILITIES),
        limits=Limits(
            page_default=PAGE_DEFAULT,
            page_max=PAGE_MAX,
            max_body_bytes=api.max_body_bytes,
            max_stream_clients=api.max_stream_clients,
            auth_failures_per_minute=limiter.limit,
            auth_lockout_s=int(limiter.lockout_s),
        ),
        retention=Retention(
            replay_s=api.replay_retention_s,
            idempotency_s=api.idempotency_retention_s,
            operation_deadline_s=api.operation_deadline_s,
        ),
    )


@router.get("/me", response_model=Me)
async def me(auth: Authenticated = Depends(current)) -> Me:  # noqa: B008
    """The authenticated client and the capabilities its token carries now."""
    return Me(
        client_id=auth.client.id,
        name=auth.client.name,
        capabilities=[cap for cap in CAPABILITIES if cap in auth.principal.capabilities],
        token_expires_at=rfc3339(auth.claims.expires_at) or "",
    )
