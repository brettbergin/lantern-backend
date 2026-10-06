# The remote API

The daemon's remote operations API: how to switch it on, register a client,
mint a token, and drive a run from admission to its result without a shell
on the host. This page is the operator's and integrator's reference; the
contract of record is the OpenAPI document the listener publishes at
`/v1/openapi.json` (the committed copy is [`openapi.json`](openapi.json)).
The design it implements is the [remote API spike](spikes/remote-api.md);
the internals are in [architecture.md](architecture.md#the-remote-api-listener).

- [Installation](#installation)
- [Clients and tokens](#clients-and-tokens)
- [Local collaboration](#local-collaboration)
- [Capability discovery](#capability-discovery)
- [Resources and ids](#resources-and-ids)
- [Endpoint catalog](#endpoint-catalog)
- [Commands, operations and idempotency](#commands-operations-and-idempotency)
- [Following the work](#following-the-work-events-sse-and-the-websocket)
- [Errors](#errors)
- [Limits](#limits)
- [Isolation](#isolation)
- [Recovery procedures](#recovery-procedures)
- [What is not offered](#what-is-not-offered)
- [Readiness criteria for a hosted service](#readiness-criteria-for-a-hosted-service)

## Installation

The API runs **inside `lantern daemon`**: one process owns execution, and the
listener is a second way in, never a second scheduler. Install the `api`
extra's packages into the home's venv and switch it on:

```bash
~/.lantern/bin/uv pip install --python ~/.lantern/venv/bin/python \
  'fastapi>=0.115' 'uvicorn[standard]>=0.30' 'pyjwt[crypto]>=2.9'
```

```toml
# $LANTERN_HOME/config/lantern.toml
[api]
enabled = true
bind = "127.0.0.1"        # loopback by default; your reverse proxy terminates TLS
port = 8420
# trusted_proxies = ["10.0.0.2"]  # a proxy on another host; a local one is believed unset
```

A daemon with `enabled = true` and the extra missing refuses to start and
names the extra. The listener speaks plain HTTP and never terminates TLS:
put a reverse proxy in front for anything beyond the host. A proxy on the same
host is believed while `trusted_proxies` is unset; list one elsewhere there
([deploy.md](deploy.md#the-remote-api-behind-a-proxy)).
Every `[api]` key is in the [user guide's knob table](user-guide.md#configuration).

`GET /health/live` answers as soon as the process is up; `GET /health/ready`
answers `503` until recovery has established execution ownership and the
daemon takes commands, then `200` with the daemon's `generation` and the
public chronology's lag.

## Local collaboration

The API can also serve a single-user local product such as Lantern. This is an
additive layer around the existing run API: it uses the daemon's store,
concierge, operation controls, schedules, events, artifacts, and usage instead
of starting another scheduler or opening another SQLite writer.

The first local user owns the workspace; later users register with an invite.
`POST /v1/auth/local/register` creates a profile and its scoped API principal;
a `username` beginning
with `usr_` is refused (`422 invalid_request`), since that is how the user
ids in `GET /v1/users` read and no name may stand in for one; `/v1/auth/local/login` returns
the same short-lived access and rotating refresh tokens as the existing client
credential flow. Existing machine clients and all existing routes keep their
original behavior.

### Work in the channel that asked

With `collaboration.channel_runs` work lives in the channel it was asked for
in (`docs/spikes/work-channels.md`). A job admitted from a chat turn, or
through the API naming a `channel_id`, is bound to that channel: its
attempts, the plan, verdicts, delivery and notices its runs post
(`collaboration.run_progress`) and the imported milestones all land there,
under one ledger, so each moment is said once. Whatever is asked there next
runs there too — a retry, a resume, or new work once the last has ended.
Work nobody asked for in a channel (a labelled issue, a schedule's tick, an
admission that names no channel) has no conversation to live in, so it keeps
the system-created, workspace-visible channel `collaboration.external_work`
describes below.

A channel works one run at a time. While a run it asked for is queued or
running:

- a plain message there is a conversation about that run: the turn goes to
  the model with the read tools (so "how is it going?" is answered from the
  run's own record) and a `steer_run` tool for the live run. A message that
  tells the run what to do differently is handed over through it, and the
  turn comes back with `steered_run_id` as a mention steer does
  (`collaboration.mention_steering`); the instruction is the same recorded
  operation `POST /v1/runs/{id}/steering` makes. Whoever may post in the
  channel may steer it;
- a turn that picks a runner (`intent` `code` or `workload`) is answered,
  but cannot start work: the start tools are withheld, the reply says the
  run has to end first, and the Workload runner's binding choice queues
  nothing while the channel is busy;
- an admission naming the channel (`POST /v1/items` with `channel_id`) is
  refused `409 already_in_progress`, with nothing queued. Replaying the
  admission that made the live item is still a replay.

`@agent` still addresses that agent's lane, and `/stop` stops the channel's
work, after which the channel is free again.

Releases 2.1.44 to 2.1.47 advertised `collaboration.work_channels` instead
and moved a chat's job into a channel of its own, leaving a `work_handoff`
message behind. Those channels and messages stay readable; nothing new is
written that way, and a job bound to such a channel runs its next attempt
in the channel that asks for it.

A job bound before this release keeps the chat it was bound to.

### Sign-in through an OpenID Connect provider

With `[api.oidc] enabled = true` (feature `auth.oidc`), a browser client signs
people in through the provider. `GET /v1/auth/providers` needs no token and
answers:

```json
{"local": true, "assistant_name": "Lantern",
 "oidc": {"id": "authentik", "label": "Authentik",
          "authorize_url": "https://auth.example.com/application/o/authorize/",
          "client_id": "lantern", "scopes": ["openid", "email", "profile"],
          "end_session_url": "https://auth.example.com/application/o/lantern/end-session/",
          "native_redirect_uris": []}}
```

`oidc` is `null` when the section is off or the provider's discovery document
cannot be read (a failed read is retried at most every 30 seconds).
`native_redirect_uris` lists the private-use scheme redirects (RFC 8252
section 7.1, such as `com.example.app:/oauth2/callback`) a native app may
present instead of the web redirect; with at least one configured,
`GET /v1/capabilities` also lists the feature `auth.oidc.native`. The client
runs Authorization Code + PKCE against `authorize_url` with its own `state`,
`nonce` and `code_challenge`, then posts the code, without a bearer token:

```bash
curl -s -X POST http://127.0.0.1:8420/v1/auth/oidc/token \
  -H 'Content-Type: application/json' \
  -d '{"provider":"authentik","code":"…","code_verifier":"…","redirect_uri":"https://lantern.example.com/auth/callback","nonce":"…"}'
```

The daemon redeems the code at the provider's token endpoint as the
confidential client (`client_secret_basic`, or `client_secret_post` when that
is all the provider offers), validates the ID token (an asymmetric algorithm
from `algorithms`, the provider's published key, `iss`, `aud`, `exp`/`iat`/`nbf`
within `leeway_s`, `azp` when present, a `sub`, and a `nonce` equal to the
request's), creates the account on a first sign-in, and answers with the same
`TokenResponse` a local login returns; refresh and revoke work as for any
other client. Refusals: `400 oidc_invalid_request` (unknown `provider`, or a
`redirect_uri` that is not exactly one of `redirect_uris` or
`native_redirect_uris`; the provider is not called), `401 oidc_exchange_failed` (the provider refused the code, or the ID
token did not check out; the message is generic), `403 oidc_not_allowed`
(outside `allowed_groups`), `403 oidc_account_disabled` (inactive, or removed
from the workspace), `403 oidc_not_provisioned` (unknown person with
`auto_provision = false`), `409 oidc_account_conflict`
(a concurrent first sign-in; retry), `429 too_many_attempts`, and
`503 oidc_unavailable` (discovery, keys or token endpoint unreachable, or the
client secret is not set). See the [user guide](user-guide.md#sign-in-with-an-oidc-provider-authentik)
for the configuration and role mapping.

| Resource    | Routes                                                         | Purpose                                                      |
| ----------- | -------------------------------------------------------------- | ------------------------------------------------------------ |
| Profile     | `GET/PATCH /v1/users/me`                                       | Local identity and timezone                                  |
| Agents      | `/v1/agents[/{slug}]`, `POST /v1/agents/{slug}/archive`        | Built-in, configured and saved agents; saved ones are edited |
| Teams       | `/v1/teams[/{id}]`                                             | Durable named groups of agent roles                          |
| Memories    | `/v1/agents/{slug}/memories[/{id}]`                            | What each agent keeps beyond one conversation                |
| Channels    | `/v1/channels[/{id}]`                                          | Revisioned conversation containers; deletion tombstones them |
| Messages    | `GET /v1/channels/{id}/messages`, `PUT .../{message}/reaction` | Ordered history and persistent message feedback              |
| Turns       | `POST /v1/channels/{id}/turns`, `GET .../{turn}`               | Idempotent input acceptance and durable completion state     |
| Preferences | `/v1/prompts`, `/v1/prompts/definitions`                       | Prompt context saved for the local user                      |
| Workflows   | `/v1/workflows[/{id}]`                                         | Workflow metadata used by the Lantern management screen      |
| Connections | `/v1/connections`, `/v1/connections/services`                  | Redacted status and owner management of host integrations    |

### Agents

`GET /v1/agents` lists every agent a client can address, in merge order: the
built-ins (Lantern and the planner, builder, critic and operator), then the
operator's `[[agents]]` from `lantern.toml`, then the agents people saved.
`GET /v1/agents/{slug}` also answers an alias and a retired built-in name.
The assistant that speaks as the product is always the slug `concierge`.
Under its shipped name it is listed as `Concierge`, as it always has been;
an operator who renames it (a `[[agents]]` entry for `concierge` with a
`name` and `aliases`) has it listed under that name, and its persona, the
other agents' prompts, `GET /v1/prompts/definitions` and the refusals that
name it use the same name. Before anyone signs in, `GET /v1/auth/providers`
reports it as `assistant_name` (`"Lantern"` unless renamed).
Beside the original fields, each entry carries its identity (`avatar`, a
`#rrggbb` `color`, `aliases`), its narrowing (`roles`, `tools`, `skills`,
`mcp`, `credentials`, `interests`, `can_start`, `max_runs_per_day`),
`enabled`, `source` (`builtin`, `config` or `user`), `editable` and
`revision`. Clients that see `agents.registry` among the capability
features may edit saved agents; the fields are defaulted, so older clients
keep reading the same shape. `GET /v1/agents?include_disabled=true`, which
needs `collaboration:write`, also lists disabled and archived agents and the
retired built-in names, so a client can find an agent that was switched off
and send `PATCH` with `enabled: true` to switch it back on (an archived agent
stays archived).

| Method  | Path                        | Body                                   | Result                              |
| ------- | --------------------------- | -------------------------------------- | ----------------------------------- |
| `POST`  | `/v1/agents`                | the agent spec (`slug`, `name`, ...)   | 201, the agent at `revision` 1      |
| `PATCH` | `/v1/agents/{slug}`         | `expected_revision` and changed fields | 200, the agent at the next revision |
| `POST`  | `/v1/agents/{slug}/archive` | none                                   | 200, the agent with `enabled` false |

All three need `collaboration:write`. An agent belongs to the person who
saved it: `PATCH` and archive are theirs and a workspace owner's or
admin's, and anyone else answers 403 `agent_forbidden`. Letting an agent
start work on its own is the operator's to grant: a `POST` whose
`can_start` is not empty, or a `PATCH` whose `can_start` adds a kind the
agent does not already have, needs a workspace owner or admin (for a plain
API client, `daemon:manage`) and answers 403 `agent_forbidden` for anyone
else, with nothing saved; the agent's owner may still narrow or clear
`can_start`, or send it back unchanged. `max_runs_per_day` is saved as
given, and `[agent_team] max_agent_runs_per_day` stays the ceiling where
runs start: the agent's effective daily cap is the lower of the two, so a
member lowers their agent's cap but never raises it above the operator's.
Agents declared in `lantern.toml` are not subject to either rule; they are
read-only here. A saved agent
whose stored spec no longer validates (a later release tightened a rule)
is left out of listings, answers 422 `invalid_agent` on `PATCH`, and can
still be archived. A saved agent never takes a slug or an
alias a built-in, configured or other saved agent already has, and a body
naming a key the spec does not have (a host list, an egress rule) is
refused: egress stays the operator's `[policy]`. A spec that names an
undeclared tool, `[[credentials]]` entry or `[[mcp]]` server, or has no
name, answers 422 `invalid_agent` with the reasons in `detail` and
`problems`. A `model` is checked against the configured backend's
discovered model catalog when one is cached (`lantern list-models` refreshes
it); with no catalog the name is accepted as given and a wrong one
surfaces in the run that uses it. A built-in or configured agent answers
409 `agent_read_only`; a stale `expected_revision` answers 409
`agent_revision_conflict` with `current_revision`; a slug already saved
answers 409 `agent_exists`, and an archived agent 409 `agent_archived`.
Agents and teams share one mention namespace: saving an agent whose slug or
new alias is a team's slug, or creating or renaming a team to a slug or
alias an agent (enabled, disabled or archived) answers to, is refused with
409 `slug_taken`.
An archived agent is left out of the listing, can no longer be named in a
team or mentioned, and still answers `GET /v1/agents/{slug}`.

A saved agent is addressed like a built-in: `@slug` in a turn, a
`target_slugs` entry or a team member. It answers in a chat session under
its first run role (Lantern's own session when it has none) with its own
persona. When the agent sets `model`, its turns use that model instead of
the role's configured one, and its `model_source` reads `agent.model`. An
agent's `handoff_agent` tool may address any enabled agent, saved ones
included; a disabled or archived agent is refused.

An ordinary turn talks to Lantern without host or MCP action tools. A known
`@agent`, an enabled `@team`, explicit `target_slugs`, or `intent=delegate`
records work intent and enables the corresponding concierge tools. Product
clients can instead send `intent=code` or `intent=workload` to select one of
lantern's managed runners explicitly. Lantern coordinates that turn without
seeding agent mentions as parallel chat participants: the code runner owns its
decompose/build/review/fix/CI/merge lifecycle, and the workload runner owns its
plan/execute/judge/revise/publish lifecycle. Explicit runner intents cannot be
combined with `target_slugs`. `intent=workload` is binding: a turn the model
answers inline without queueing a workload is queued by the daemon anyway,
with the turn's content as the ask under the default profile, and the reply is
the queue acknowledgement rather than the inline answer. (`intent=code` stays
with the model, which may need an intake fact — the repository, say — before
it can file the issue.)

A mention is a request to reply. It records the agent as a target and joins it
to the channel, but it no longer rewrites the turn's `intent`: a turn sent as a
`conversation` stays one. The mentioned agent keeps its read tools but is not
offered the tools that start managed work, so it answers in the chat, and says
which intent to pick when the ask needs execution, external sources, a
repository change or a produced file. A turn that may start work (`delegate`,
`code`, `workload` or `auto`) is told to answer whatever the reply itself can
satisfy — a list, an explanation, a short plan, an opinion, a judgement about
work already in the channel — and to start managed work only for those asks.
`TurnOut` carries the recorded `intent` back.

When `/v1/capabilities` lists `collaboration.lead_orchestrator`, `intent` also
accepts `auto`: the client does not know whether the ask is a question or a
piece of work, and the lead decides for that turn whether to answer, start a
code run or start a workload. `auto` accepts mentions and `target_slugs` the way
a conversation does.

On a turn that may start managed work (`intent` `code`, `workload` or `auto`),
the agents the message mentions that declare a run role are recorded on the
first entry of `participants` as `assignees`, a `role -> agent slug` map, and
work admitted from that turn is assigned from it. Other turns leave `assignees`
null.

Team members receive separate role-scoped sessions and their replies are
persisted as separate messages. Conversational peers choose their own bounded
handoffs and may return review findings to an author or coordinator for revision
and final synthesis; the transport does not encode a role sequence. Repeating a
`client_turn_id` returns the accepted turn; reusing it for different text,
targets, intent, or message identity is rejected.

Messages include a `reactions` array. User inputs receive `⏳` when accepted,
then replace it with `✅` after successful completion or `⚠` after failure or cancellation.
Clients can persist user feedback on any message with
`PUT /v1/channels/{id}/messages/{message}/reaction` and a body such as
`{"emoji": "👍", "active": true}`. Repeating the same request is idempotent;
set `active` to false to remove the reaction.

When `/v1/capabilities` lists `collaboration.message_authors`, every message
also carries an `author` object: `{"kind", "id", "display_name"}`. `kind` is
`human` (a person; `id` is the user id and `display_name` their full name, or
their username when none is set), `agent` (`id` is the agent slug and
`display_name` its registry name, so `concierge` shows as `Lantern`), or
`system` (turn error and stop notices; `id` and `display_name` are null).
Messages written before authorship was recorded report the author they always
had: the channel's user for user input, the named agent for agent replies and
handoffs, Lantern for replies and work results that name no agent. The existing
`role` and `agent_slug` fields are unchanged. Turns report the same person as
`author_id`, with `trigger` (`human` for every turn a person submits) and
`parent_turn_id` (null until agents can start turns of their own), and
`collaboration.message.created` events carry `author_kind` and `author_id`.
Each channel records its creator as its owner member; who else may open it
is described under "Channel access, members and participants" below.

The event stream records `collaboration.participant.running`,
`collaboration.tool.started`, and `collaboration.tool.completed` as the work
happens. Tool events include the channel, turn, participant index, agent slug,
tool name, and completion `ok` flag; they omit arguments and result contents.
Clients can use these events to refresh the channel's authoritative active
turns and messages, rather than infer progress from message text. Ordinary
conversation with no advertised host tools submits no host tool handler.

Successful Code issue creation or queueing records its exact repository and
issue identity on the originating participant. `GET /v1/channels/{id}/work`
projects that association even after the conversation turn finishes. Before
the source admits the issue, its state is `awaiting_dispatch`, its item ID is
a provisional `pending_code:` identity, and no controls are offered. After
admission it reports the public item/run IDs, actual stage and available
controls. Completion delivers the recorded PR link or failure to that channel.
The association is durable turn data, independent of event retention; unrelated
repositories with the same issue number do not match. No source polling or
runner behavior changes.

### Conversations for externally started work

`collaboration.external_work` advertises automatic conversations for jobs
known to the connected daemon, including issue labels, schedules, chat
bridges, API admissions and standalone persisted runs. A job without an
existing conversation gets a workspace-visible channel. An existing
association keeps its channel and access rules. Repeated attempts at the
same issue share a conversation; separate schedule occurrences do not.
The association is presentation data: it does not change the item's
admission channel, assignment, scheduling, accounting or source delivery.

Clients with this feature use `GET /v1/channels/{id}/jobs`, a list of
attempt snapshots. Each has a stable `work_id`, optional real `item_id`,
`run_id` and `turn_id`, state, source, revision, available actions and
artifacts. A run with no admitted item has no item controls. The response
also includes work awaiting Code issue admission. The existing `/work`
contract is unchanged and remains the fallback for older daemons.
An attempt whose execution record was removed remains listed with
`unavailable: true`, its recorded metadata, and no controls or artifacts.
Item list/detail responses provide a nullable `channel_id` only when the
viewer can read that conversation.

System-created channel summaries include `external_work` metadata for
sidebar status and source links without loading each transcript. Opening,
progress and result messages identify the system as their author and carry
`source_work_id`, an optional `source_run_id`, and `historical`; they do not
invent a human turn. Channel chat and live-run steering use the ordinary
permission checks. Events and artifacts resolve through the attempt's own
channel, so admitting a later attempt to a private channel does not move
an earlier attempt's history or expose the later attempt there.

The initial import includes unfinished work and terminal work from the
last 30 days. Imported messages are quiet history and do not add unread
counts. New jobs and activity remain unread until read. The scoped event
`collaboration.external_work.attention` carries `channel_id`, `work_id`,
optional `run_id`, a durable `attention_id`, `kind` (`work`, `failure` or
`action_required`), `title`, `body` and `historical: false`; clients apply
their channel preferences and browser-notification opt-in. Reconciliation
and event replay do not resend the same transition. Deleting a generated
channel hides it without recreating it on the next reconciliation.

### Admitting work for named agents

`POST /v1/items` takes three optional fields on an `issue` or `workload`
body (advertised as `intake.assignment`): `lead`, the agent that leads the
run; `roles`, an object mapping a run role (`planner`, `builder`, `critic`,
`operator`) to an agent slug; and `channel_id`, the channel the work answers
to. Each named agent must exist, be active (not disabled or archived) and
declare the role it is asked to take (`lead` for the lead); anything else is
`422 invalid_argument` naming the agent and the role, and nothing is queued
or labelled. Naming a `channel_id` also takes `collaboration:write` (and
`collaboration:read` for a workspace member's client), checked first: without
it the request is `403 forbidden`. A `channel_id` the caller cannot read is
`404 channel_not_found`. Work asked for again after its last run finished is
planned afresh from the new request's lead and roles.
A body without them admits work exactly as before.

When the item is dispatched, the daemon plans its assignment: each role takes
the agent asked for and the built-in agent otherwise, and the lead is the one
asked for or Lantern. The plan is stored with the item, and every later attempt
at the same item reuses it, even if an agent was archived since. Issues found
by polling run with the built-in team. Items read back with `lead_agent` (the
planned lead once dispatched, the requested one before) and `assignment` (the
agent in each run role, or `null` when none were named and nothing is planned
yet).

A chat turn passes its channel and, for the agents it mentioned, the run roles
they declare to the work it starts: a workload it queues carries them, and an
issue it files or labels leaves a note the polled item picks up. The note is
spent by the item it fills, so an old conversation's request is never replayed
onto work the same issue is labelled for later. A turn answered by Lantern names
Lantern as the lead. An item that names its channel is delivered there even when
its key names no message in it (as part of the channel's latest turn when it
was admitted), and never to a channel other than its own; that holds for an
issue (`code`) admission too, whether or not any turn in the channel named the
issue. With `collaboration.external_work`, a channel that has had no turn
yet receives system-authored progress and results through the job projection.
Older daemons skip that delivery and log `api.work_delivery_skipped`.
A turn-associated work result is credited to the item's lead
when it has one, and to the participant that asked otherwise.

A finished workload or tool run's files are catalogued before its work result
is written, so the first `work_result` message already names them. Its `work.artifacts` (and
each entry of `GET /v1/channels/{id}/work`) lists up to 50 available files,
ordered by path, as `{id, run_id, relpath, media_type, size}`: `id` is the
catalog identity served by `GET /v1/artifacts/{id}` and `run_id` is the public
run ID. Files the retention sweep removed are left out. The message text ends
with a `Files:` list of the same paths, for surfaces that show only text. A
result written before this field reports an empty list. The feature is
advertised as `collaboration.message_artifacts`. A code run delivers a pull
request, so its checkout is never listed and its `artifacts` stays empty.

### Files a channel can see

The same files are attached to the message itself: `MessageOut.artifacts` is
the list of `{id, run_id, relpath, media_type, size}` that message carries,
served on `GET /v1/channels/{id}/messages` and empty for every message that
carries none.

`GET /v1/channels/{id}/artifacts` lists every file the channel's messages
carry, newest message first, as `{"data": [...]}`, and
`GET /v1/channels/{id}/artifacts/{artifact_id}/content` serves one file's
bytes as an attachment. Both take `collaboration:read` and channel read
permission rather than `artifacts:read`, so a workspace member who can read
the channel can read the files delivered into it. Both resolve through that
same list, so a file the channel does not carry is `404` there whatever else
the caller may read -- including a catalogued file of a run the channel started
but never delivered here, such as a code run's checkout.
`GET /v1/runs/{id}/artifacts`, `GET /v1/artifacts/{id}` and
`GET /v1/artifacts/{id}/content` are unchanged and still take
`artifacts:read`. The channel routes are advertised as
`collaboration.channel_artifacts`.

An agent answering in a channel gets one more host tool,
`read_channel_artifact(artifact_id, offset=0, limit=64000)`. It is offered to
every participant, read-only roles included, so a critic can read the file it
is reviewing. It resolves an id only when a message in *this* channel carries
it, or when a workload or tool run this channel admitted delivered it (so it
can read a result before the message lands). A code run's checkout is refused
here exactly as the download route refuses it. It takes no path, returns UTF-8
text with a `[truncated ... call again with offset=N]` marker past the window,
and answers one metadata line for anything that is not text.

### Channel history and its summary

A turn's prompt carries the channel's history as one JSON object per line:
`{seq, author_kind, author, role, kind, content}`, plus `artifacts`
(`{id, name, media_type, size}`) on the messages that carry files. It is
bounded to 200 messages and 60,000 characters. When anything is dropped, the
history opens with a `channel_summary` line holding the channel's latest
summary, so the earlier conversation is compacted rather than lost. The
summary is written after a turn settles -- on its own thread, not in the
turn's lane, and with a bounded wait -- by one tool-less call on the
concierge's own model (`[concierge] model`), in a session belonging to that
channel alone. It covers exactly the messages that call was shown, so a
backlog too large for one excerpt is summarised over several compactions.
The first summary is written as soon as the history trims; after that it is
rewritten once 50 messages or 20,000 characters have fallen out since, not on
every turn, so the few messages between the summary and the window wait for
the next batch. Only the newest summary is kept, and its model call is charged
to the channel. It is best effort, and a channel without a summary simply gets
a shorter history.

### What a run says in its channel

A run linked to a channel posts into it under the name of the agent doing
the work, advertised as `collaboration.run_progress`. A post is a message
with `kind` `agent_update`, `role` `assistant`, `author`
`{"kind": "agent", "id": <slug>}` and `agent_slug` set to the same slug.
It carries `post_kind`, one of `plan`, `progress`, `review`, `delivery`,
`reply` or `notice`; every other message reports `post_kind` as null. When
the run names files, they are listed on the post's `work.artifacts` in the
shape described above.

A post belongs to the turn that asked for its work, the same turn that
work's result is delivered on, and to no turn at all rather than to one
from another channel. A run a channel asked for outside any turn of its
own still posts and still names its files: `work.turn_id` is null on such
a post, so a client reads it as nullable. A snapshot a run hands in that
does not fit this shape is replaced by what Lantern itself knows about
the work, and a snapshot already recorded that a later build cannot read
is reported as no snapshot: a message the reader cannot parse never costs
the channel its message list.

Each post names a dedupe key, which is what makes a replayed, resumed or
re-observed run post a moment once: the same key returns the message
already recorded rather than a second copy of it. The key belongs to its
run in its channel: one another run or another channel already used is
neither an answer for the post nor a reason to drop it. A run posts only
into the channel that asked for it; a post naming any other channel is
dropped and nothing is written. A channel that was deleted receives
nothing. A silenced channel drops the running commentary
and still hears the posts that end a run: `delivery` and `notice`.

Clients read posts with the message history they already poll, or
incrementally with `GET /v1/channels/{id}/messages?after=<sequence>`, and
see each one as a `collaboration.message.created` event carrying
`post_kind` and the run's public `run_id`, the id
`GET /v1/runs/{id}` answers to. The events of a run a channel asked for, and the
run's catalogued files, belong to that channel: a member who can open it
sees them. A workspace member without `artifacts:read` may list and download
the files of a run a channel they can open asked for, through
`GET /v1/runs/{id}/artifacts` and `GET /v1/artifacts/{id}[/content]`; every
other run's files, and an id nobody catalogued, answer the same `403`
naming `artifacts:read` that they always did.

A `post_kind` Lantern does not know is never stored: the post is dropped and
its dedupe key stays free. One recorded by a later build reads back as null.

Discovery lists lantern's five native roles: `concierge`, `planner`, `builder`,
`critic`, and `operator`. Chat resolves their models through the existing
configuration, using the `concierge`, `decompose`, `build`, `review`, and
`operator_plan` phases respectively. `phase_models` also reports the remaining
engine phases for each role. The same refreshed model settings drive dispatch.
Legacy product role names remain addressable for existing saved teams and clients
but are excluded from discovery.

Native chat shares the concierge transport and its host-tool boundary. Actual
code changes and workload execution use managed runs; a chat session has no
checkout, editor, or shell. Critic chat receives only explicitly allowed read-only
host tools and no MCP servers. The host rejects tools outside that allowlist.

Delegated chat also exposes `handoff_agent(agent_slug, message)`. An agent can
ask any other native role for help; plain `@mentions` in its prose do not
dispatch work. The tool queues a peer response in the same turn after already
queued participants, without waiting inside the current response. The peer
receives the original user request, the scoped peer request, and prior chat
replies. It may explicitly hand back to the sender for synthesis.

Handoffs append durable `agent_handoff` messages and
`collaboration.handoff.queued` events. Per-member progress includes
`requested_by`, `parent_index`, `request`, and `read_only`. Handoff events and
concierge tool logs omit the peer request text. Repeating the same sender/recipient/request
within one response returns the original receipt without another invocation.
The user's original targets and idempotency identity remain unchanged.

Each response can request two peers, with six additional responses total and
three handoff levels per user turn. A role cannot hand off to itself. Critic
and every descendant of a read-only response retain the read-only host-tool
allowlist and no MCP servers, regardless of the recipient role. A peer request
does not grant new human approval. Ordinary conversation has no handoff tool.
Stop skips queued peers as well as team members. Restart recovery considers all
dynamic participants, and never replays an interrupted handoff.

`GET /v1/channels/{id}/turns?active_only=true` reports active turns and per-member
progress. `POST /v1/channels/{id}/turns/{turn}/cancel` stops queued responses.
An in-flight member may finish and its result is preserved; remaining members
are skipped. This does not undo effects or cancel previously dispatched runs.
Interrupted cancellation settles durably as cancelled on restart.

`GET /v1/events?latest=true&limit=200` reads the newest bounded snapshot, in
ascending sequence order, including after retention has pruned earlier events.
It cannot be combined with `after`; existing cursor replay and streams retain
their behavior. HTTP requests produce structured `api.request` logs with route
templates, status, generated trace IDs, and duration. Request bodies, query
strings, authorization headers, and caller-provided request IDs are not logged.

Turns execute in admission order through the single concierge. A team's members
run sequentially, each receiving durable channel history and the earlier members'
replies. The context is bounded to the latest 200 messages and 60,000 characters;
it excludes other channels and later queued user inputs. History also rebuilds
context after a provider session is lost or the daemon restarts.

Before serving requests after a restart, the API resumes accepted turns that
never started. A running turn with every expected reply already persisted is
completed without another invocation. Other interrupted running turns fail with
a durable explanation: their actions may already have occurred, so they are not
automatically replayed. Provider failures also appear in channel history.
Deleting a channel prevents queued turns and remaining team members from starting
and discards late replies. It does not undo an action already running.

Each agent has a long-term memory, reviewed and edited through
`/v1/agents/{slug}/memories` (advertised as `agents.memory`; an alias names the
same agent, and an unknown agent is `404 agent_not_found`). `GET` needs
`collaboration:read`; `POST`, `PATCH` and `DELETE` need `collaboration:write`.
A memory is `{id, agent_slug, kind, content, source_channel_id, source_run_id, source_message_id, author, pinned, created_at, updated_at, last_used_at, revision}`,
where `kind` is `fact`, `preference` or `procedure` and `author` is
`agent:<slug>` or `user:<id>`. A memory with no `source_channel_id` is global;
one learned in a channel is listed only with `?channel_id=` naming that channel
(or a channel whose `visibility` is `workspace`). `include_private=true`
lists every memory for a plain API client or a workspace owner or admin; for
any other member it adds only the memories from channels that member can read
(workspace channels and the ones they created or belong to). A member never
sees a memory from a private channel they cannot read. `q` keeps memories
sharing a word with it. Listing does not change `last_used_at`.

`POST {content, kind?, pinned?, channel_id?}` stores a memory authored by the
caller; the text is cut to `[memory] max_item_chars`, and past
`[memory] max_items_per_agent` the agent's oldest unpinned memory is dropped
(`409 memory_full` when every one is pinned). `PATCH {content?, pinned?, expected_revision}`
answers `409 revision_conflict` for a stale revision. `DELETE` forgets the
memory (a soft delete). Both read the caller's channel access first: a
memory from a private channel the caller cannot read answers the
`404 memory_not_found` an unknown id answers, so it is neither changed,
forgotten nor read back. With `[memory] enabled = false`, `POST` answers
`409 memory_disabled`. Changes write `agent.memory.created`, `.updated` and
`.deleted` events that name the memory, its agent and its source channel but
never its text.

A mentioned agent's chat persona carries the memories it may see in the turn's
channel (nothing is added when it has none). An agent whose `tools` list names
`memory`, or a person's own agent with no `tools` list, is also given
`remember`, `recall` and `forget` in chat; a read-only peer turn gets `recall`
alone. What it keeps is authored `agent:<slug>` and scoped to the channel and
message of the turn. Built-in agents and `[[agents]]` entries with no `tools`
list get no memory tools. In a run, a custom agent's memory block is taken
when the run is planned and kept across a resume, and an agent whose `tools`
names `memory` gets the same tools, writing with the run's id and channel; a
read-only session, and a critic whatever its session, gets `recall` alone, as
a read-only chat turn does. With `[memory] enabled = false` no memory reaches
a prompt and no tool is offered. A run that a delegated decision depends on
binds its agents with no memory at all — no block, no memory tools, resume
included: today that is the breakdown of a plan whose `advance` is `auto`. A
breakdown of a `manual` plan, and every other run, takes memories as above.

A run started from a channel keeps what its agents remember for that channel.
A run with no channel — one a labelled issue, a schedule or the CLI started —
has no channel to keep it for, so what its agents remember there is
**workspace-global**: that agent recalls it in every channel, for anyone who
can address it. This is deliberate, so a run's agent can use next week what it
learned this week wherever the next ask arrives. The `remember` tool says so
in its own description whenever the agent is working without a channel, and
`GET /v1/agents/{slug}/memories` shows such a memory with no source channel.
Give a run a channel when what its agents keep should stay in one place.

A memory's text stays out of the daemon log: a `remember` or `recall` tool
call is logged by length, not by content, as `agent.memory.*` events are
logged by id. The log is one stream for the whole installation, and any agent
can read it from any channel through `daemon_log`.

### Connections

When capability discovery includes `collaboration.connections.manage`, a
workspace owner can configure GitHub, GitLab, Slack, Discord and Mattermost
through `PUT /v1/connections/{service}`. The body has `settings` (the service's
nonsecret URL and channel fields), `credentials` (write-only tokens), and
`activate`. Only the listed fields are accepted. Secrets are written to the
home's private `config/secrets.env`; other settings go to its
`config/lantern.toml`. Both save paths keep timestamped backups; secret
backups remain mode `0600`. The response contains
only presence flags and nonsecret settings. Existing `POST /v1/connections`
and `PATCH /v1/connections/{id}` clients still receive
`operator_managed_connection` instead of accidentally using the old mutation
shape.

`GET /v1/connections` reports `configured` (settings and required credentials
are present), `active` (the running daemon selected that service),
`restart_required`, and `status`. A saved configuration begins as
`disconnected`: the list never calls it connected solely because a token or
channel ID exists. `POST /v1/connections/{id}/test` contacts the provider and,
for chat services, checks channel access. A successful check verifies those
requests; it does not prove that the long-lived bridge is running. A failed
check reports a generic refusal without returning provider bodies or secrets.

Changes take effect after a daemon restart. `DELETE /v1/connections/{id}`
clears credentials owned by `secrets.env`; for a chat bridge it also removes
its channel selection. For a forge it leaves repository and VCS assignments
intact, so existing repositories are never silently moved to another forge.
Secrets supplied outside the managed file must be removed by the host operator;
the API refuses to claim their removal. Gitea remains visible but unavailable
until it has an execution backend. GitHub App credentials remain host-managed;
the catalog identifies that auth method, and its check directs the operator to
`lantern doctor` rather than claiming a PAT check verified the App installation.

### Repository discovery

When capability discovery includes `repositories.discover`, a workspace owner
can ask `GET /v1/repositories/available` which repositories the host's forge
credential can see, so a client offers a list to pick from instead of a box
to spell `owner/name` into. `forge` defaults to `[vcs] kind`. The answer is
`{forge, credential: {mode, login}, data: [...], truncated}`: `mode` is `pat`
(a personal token, with the account it belongs to) or `app` (a GitHub App
installation; its own repository list, no login), and every entry carries
`repository`, `owner`, `name`, `private`, `archived`, `default_branch`,
`url` and `configured` (already declared to this daemon). The listing is read
from the forge now, on the host, with the same credential snapshot the
connection check uses (`GH_TOKEN` / `GITHUB_TOKEN`, else the App; the
`[vcs] token_env` variable for GitLab); it walks at most two thousand entries
and says `truncated` past that. Nothing is written. Without a credential the
route answers `409 discovery_unavailable` naming what to set; a forge that
refuses the credential is `502 provider_error` with the status and never the
body; an unreachable one is `502 provider_unreachable`. The route is the list to
choose from; registering one is the next section.

### Repositories

Where a repository is registered is the daemon's database, advertised as
`repositories.manage`. The file's `[[vcs.repos]]` entries are imported at
first sight (once; a removed registration keeps its row, so the file's copy of
that name is not imported again), and from then on `daemon:manage` clients
change the registration live:

| Route                          | Body                                              | Result                                                      |
| ------------------------------ | ------------------------------------------------- | ----------------------------------------------------------- |
| `POST /v1/repositories`        | `{repository, forge?, enabled?, deliver_base?}`   | `201 {repository, message, operation}`; `repo.add` recorded |
| `PATCH /v1/repositories/{id}`  | `{enabled?, deliver_base?}`, only the fields sent | `200`, the repository as it stands; `repo.update` recorded  |
| `DELETE /v1/repositories/{id}` | none                                              | `200 {repository: null, message, operation}`; `repo.remove` |

`repository` is `owner/name` (`group/subgroup/project` on GitLab); `forge`
defaults to `[vcs] kind`; `deliver_base: null` on a `PATCH` clears it. A name
registered already (case-insensitively), one that is not a repository on its
forge, or an unknown forge is `422 invalid_argument`; an unknown id is `404`.
Every entry of `GET /v1/repositories` now carries `source` (`config` for an
imported entry, `api`), `created_by`, `created_at` and `restart_required`.

Every entry also carries `labels`: whether the repository carries the labels
the loop applies — the seven lifecycle labels under this repository's own
names, the planning level labels where `[planning]` is on for it, and the
follow-up label. `state` is `compliant` (it carries every one
of them, as of `checked_at`), `incomplete` (`missing` names the ones it does
not), or `unknown` — nobody has been able to look yet, the forge would not
answer, or the configured names have changed since the last look. `unknown`
is never reported as compliant, and each entry of `labels.labels` carries
`present: null` under it rather than a guess. The daemon reads one
repository's labels back per tick, at most one reading per repository per
`[daemon] label_check_interval_s`, so the answer is current without a client
asking the forge anything.

`POST /v1/repositories/{id}/labels/sync` (`daemon:manage`) creates the ones
the repository is missing and answers `200 {repository, labels, created, message, operation}` with `repo.labels_sync` recorded — the same work
`lantern init-repo` does from the host, through the daemon's own forge
sandbox. It reads the repository first, so a repository that already carries
every label is left untouched and reports itself compliant with `created: []`. A daemon with no forge sandbox refuses with `409 not_eligible` naming
the command that works from the host; a forge that would not answer is `503 source_unavailable`, and the last reading stands, dated, rather than being
overwritten with a guess. The socket takes it as `repository.labels_sync`
(target `repo_…`).

A registration takes effect in what the daemon *admits* at once: intake,
the engine's narrowing, the concierge and this catalog all answer for it.
What the daemon *polls* was built at start, so a registration that changes
the enabled set says `restart_required` (and the reply's `message` says
so); `POST /v1/daemon/restart` applies it. The file keeps a repository's
other settings — labels, templates, workspace, sandbox packages, model
overrides — folded under the registration of the same name; a new entry in
the file is registered at the next start, and the file's `enabled` /
`deliver_base` are only the initial values (`lantern doctor` names an entry
the file still spells differently). The socket takes the same commands:
`repository.add` (params), `repository.update` and `repository.remove`
(target `repo_…`).

### Plans

Planning turns a larger effort into issues the loop can work (see the
[spike](spikes/work-planning.md)). It is advertised as `planning` when a
configured forge can hold a plan, together with `planning.clarify` (the
planner's clarifying questions and the answers route), epic runs as
`planning.run`, the `advance` switch with each node's actors and review
as `planning.advance`, the daemon moving an `auto` plan forward under
an owner's grants as `planning.driver` (see "Plans that advance
themselves" below), and the planner drafting plans from goals as
`goals.proposing`. A **plan** is a tree of **nodes**: an
initiative breaks into epics, an epic into tasks. A plan starts at an
initiative (its home repository) or at a lone epic. Every plan, drafts
included, is shared across the workspace: `runs:read` reads every one.
`plans:create` (members hold it) drafts and edits; `plans:publish` (admins
and owners) publishes to the forge and edits, attaches and detaches its issues.

| Route                                                                 | Body                                                              | Result                                                                                     |
| --------------------------------------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `GET /v1/plans`                                                       | `?repository=&level=&state=&limit=&cursor=`                       | `200 {data: [plan summary], next_cursor, has_more}`, most recent first, at most 200 a page |
| `POST /v1/plans`                                                      | `{level, repository, title, goal?, advance?, …}`                  | `201`, the plan with its root node                                                         |
| `GET /v1/plans/{id}`                                                  | none                                                              | `200`, the plan and every node                                                             |
| `PATCH /v1/plans/{id}`                                                | `{expected_revision, advance?, …sections}`                        | `200`, the root node's sections edited, the switch flipped                                 |
| `DELETE /v1/plans/{id}`                                               | `?expected_revision=`                                             | `200 {id, outcome: deleted \| archived}`                                                   |
| `POST /v1/plans/{id}/nodes`                                           | `{expected_revision, parent_id, title, repository?, …}`           | `201`, the plan; `Location` names the new node; `409 level_full` at the cap                |
| `PATCH /v1/plans/{id}/nodes/{node_id}`                                | `{expected_revision, position?, forge_version?, …sections}`       | `200`, the plan (a published node: its issue written)                                      |
| `DELETE /v1/plans/{id}/nodes/{node_id}`                               | `?expected_revision=`                                             | `200`, the plan without the node and its subtree                                           |
| `POST /v1/plans/{id}/nodes/{node_id}/breakdown`                       | `{expected_revision, note?, channel_id?}`                         | `202 {plan_id, node_id, item, operation, created}`, a `plan` run queued                    |
| `POST /v1/plans/{id}/nodes/{node_id}/answers`                         | `{expected_revision?, answers: {id: {value?, text?}}, skip?}`     | `200 {plan, run_id, resumed}`, the waiting run back in the queue                           |
| `POST /v1/plans/{id}/nodes/{node_id}/approve`                         | `{expected_revision, node_ids?}`, `Idempotency-Key` optional      | `200`, the plan with those children approved                                               |
| `POST /v1/plans/{id}/nodes/{node_id}/publish`                         | `{expected_revision}` and an `Idempotency-Key` header             | `200 {plan, results, operation_id, replayed}`                                              |
| `POST /v1/plans/{id}/nodes/{node_id}/attach`                          | `{expected_revision, repository?, number?, url?}`                 | `200 {plan, node_id, linked, reason}`                                                      |
| `POST /v1/plans/{id}/nodes/{node_id}/detach`                          | `{expected_revision}`                                             | `200`, the plan with the child detached                                                    |
| `POST /v1/plans/{id}/sync`                                            | none                                                              | `200`, the plan reconciled from the forge now                                              |
| `POST /v1/plans/{id}/drift/ack`                                       | `{expected_revision, node_ids?}`                                  | `200`, the plan with that drift marked seen                                                |
| `POST /v1/plans/{id}/nodes/{node_id}/replan/approve`                  | `{expected_revision, entry_ids?}` and an `Idempotency-Key` header | `200 {plan, results, operation_id, replayed}`                                              |
| `POST /v1/plans/{id}/nodes/{node_id}/replan/discard`                  | `{expected_revision, entry_ids?}`                                 | `200`, the plan without those re-plan entries                                              |
| `POST /v1/plans/{id}/nodes/{epic_id}/run`                             | `{expected_revision}` and an `Idempotency-Key` header             | `201`, the epic run; a replay is `200`                                                     |
| `GET /v1/plans/{id}/nodes/{epic_id}/run`                              | none                                                              | `200`, the epic's most recent run                                                          |
| `POST /v1/plans/{id}/nodes/{epic_id}/run/pause`, `/resume`, `/cancel` | none; an `Idempotency-Key` header                                 | `200`, the epic run                                                                        |
| `POST /v1/plans/{id}/nodes/{task_id}/run/retry`, `/skip`              | none; an `Idempotency-Key` header                                 | `200`, the epic run                                                                        |

A node's sections are `title`, `goal`, `context`, `acceptance_criteria` (a
list), `non_goals` and `constraints`; a task also carries `kind` (`code` or
`workload`), `workload_profile` (a configured profile, workload tasks only),
`verify_commands` (code tasks only) and `depends_on` (sibling task ids, never
a cycle). A child is always one level down; an epic may target any
plannable repository (its initiative's home by default), and a task lives in
its epic's. A node's `state` is `draft`, `proposed` (the planner wrote it
and nobody has touched it), `approved` or `published`; editing a proposed or
approved node makes it a draft again, and a published node carries `forge: {number, url, state, version}` and editing its sections writes its issue (below). A plan's `state` is
`draft` until something of it is published, then `published`; deleting a
published plan archives it (its issues stay) rather than deleting it. Every
summary carries a `rollup`: epics, tasks, tasks the forge has closed, and
published nodes.

With `planning.generated_root`, `POST /v1/plans` treats every form section
as an inference brief. The plan detail exposes that brief as `input` and
`generation_pending: true`; its root is an unplanned placeholder containing
none of the person's prose. The summary uses the brief's title until the
planner authors an issue title. Editing the pending root updates `input`,
not issue content. Breaking it down uses the brief, repository and existing
clarification answers to generate both the root and its immediate children
atomically as `proposed` nodes. Subsequent breakdowns preserve the reviewed
parent and generate the next level. The original input stays available as
context when an initiative's epics are expanded into tasks.

Publishing while `generation_pending` is true refuses with
`409 generation_required`. A brief changed during inference refuses delivery
with `stale_input`, including when a run resumes after saving its output.
The user can generate again against the new brief. On upgrade, existing
unpublished, person-authored roots move into `input`, retaining their trees
and requiring generation before publishing; published and archived content
is preserved.

**Who a node is from, and whether a plan advances itself** (feature
`planning.advance`). Every node carries three read-only fields, each a
principal's id — the one the same act's event names as its actor — or
`agent:<slug>`, or `null` where nobody is recorded:

| Field          | Holds                                                                                                                                                                                                                                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `proposed_by`  | Who the node's content is from: the person who drafted the plan or added the node; for a node a `plan` run proposed (the generated root, its children, a re-plan's addition), the planner agent bound to that run. `null` for an issue adopted from the forge and for a run that named no agent. An edit keeps it. |
| `approved_by`  | Who approved the node for publishing (`approve`, or approving the re-plan entry that added it). Editing the node makes it a draft again and clears this.                                                                                                                                                           |
| `published_by` | Who published the node to the forge. A root is published with its level, so it carries this and no `approved_by`.                                                                                                                                                                                                  |

Nodes written before these were kept read `null` in all three. A node may
also carry `review`, a reviewer's verdict on its **level** (its children):
`{run_id, verdict: approve | escalate, reasons, digest, reviewed_by, at, current}`. `digest` names what was reviewed and `current` is whether the
level still reads so: an edit of a child's title or sections, a child added,
removed, moved or replaced, or a changed repository makes it `false`, and a
review that is not current says nothing about the level as it is now.
Approving and publishing the level do not move it. The daemon writes
`review`; no route does. A breakdown of a plan whose `advance` is `auto`
fills it: the run's critic reviews the proposed level before it is
delivered, and the verdict lands on the node in the proposal's own write,
`current: true` until the level changes (see the breakdown route below).
`reviewed_by` is the critic, `agent:<slug>`; a review whose answer was
unusable reads `escalate` with the reason `the reviewer did not return a usable verdict`. A `manual` plan's breakdown never writes one.

A plan carries `advance`: `manual` (the default — a person takes every
step) or `auto`, and `goal_id`, the goal it was proposed from (`null` for a
plan a person drafted; read-only). `advance` is set on `POST /v1/plans` and
flipped on `PATCH /v1/plans/{id}`, alone or with sections, on a published
plan too. Setting it takes **`plans:publish`** on top of the route's
`plans:create`: a caller without it who names a value the plan does not
already have (anything but `manual` on create) is `403 forbidden` with
`capability: plans:publish`, and nothing in the request is written; the
same request without `advance`, or naming the value the plan has, works as
before. A flip is `plan.node.changed` with `node_id: null`,
`change: advance`, `advance` and `before`, under whoever flipped it.
`goal_id`, `review` and the three actor fields are refused as unknown
fields (`422`) in any request body. Where `/v1/capabilities` lists
`planning.driver`, an `auto` plan is moved forward by the daemon under the
grants (below); a `manual` plan is never touched by it, and neither is an
`auto` one whose step no enabled grant covers. Lantern's default grants
(see [Delegation](#delegation)) cover the plan steps, so on a daemon that
still has them, setting a plan to `auto` is what lets it move.

**Plans that advance themselves (`planning.driver`).** On every tick the
daemon is not held, its plan driver takes the next step of each `auto` plan
that is not archived, as an agent and only when a grant
([Delegation](#delegation)) allows it: it queues a node's **breakdown**
(the `planner`, `plan.breakdown`) when the node has no children yet; it
**approves** the node's `draft` and `proposed` children (the `critic`,
`plan.approve`); once `[delegation] publish_delay_s` (900 s by default) has
passed since the level was approved, it **publishes** it (the `critic`,
`plan.publish`); and it **starts the epic run** of a published epic whose
tasks are all on the forge and that never ran (the `critic`, `plan.run`).
A node below the root is broken down once it is on the forge. One act per
plan per tick, one forge write per tick in all. What a client sees is the
same record a person's step leaves, under the agent:

- each step is an operation like a person's (`item.admit` for a breakdown,
  `plan.approve`, `plan.publish`, `plan.run`) whose `actor` is
  `{"kind": "agent", "id": "agent:critic", "display": "the critic agent", "via": "agent"}` (or the planner);
- the nodes' `approved_by` and `published_by`, and the epic run's
  `started_by`, read `agent:critic` (`started_by_display`: "the critic
  agent"); a breakdown's children read `proposed_by: "agent:planner"` as
  they always did;
- every step considered — allowed, denied or escalated — is a row of
  `GET /v1/decisions` naming the plan, the node, the item or epic run, the
  operation when it was taken, and the facts it was judged on.

Anything the grants do not cover waits for a person and is one `escalate`
row, written once while the situation stands: no grant, a condition not
met, a `require_review` grant with no current review (an edit of a child
makes it stale), the critic's verdict `escalate`, a breakdown that already
ran for the node and left nothing (the daemon never queues another), a
repository that cannot hold the plan, or a step the forge refused (tried
again after `[daemon] poll_interval_s`, then twice as long after each failure
in a row, at most an hour apart). It is resolved `acted`
when the step happens — taken by the daemon or by a person through any
route — or `superseded` when the level changes under it. A person can step
in at any point: flip `advance` to `manual` (the next step is not taken),
or take the step themselves through the usual routes.

**Plans proposed from goals (`goals.proposing`).** Where
`/v1/capabilities` lists `goals.proposing` and `[delegation] propose_every`
is set (seconds; `0`, the default, is off), the daemon drafts a plan for
each `active` goal that has no plan still open (not archived, and not done:
every epic closed on the forge), at most once per `propose_every` per goal
and one per tick across all goals, as the `planner` under a `plan.propose`
grant. What a client sees:

- a new draft plan with `created_by: "agent:planner"`, `advance: "auto"`,
  `goal_id` the goal's, and a root to be generated from a brief: the goal's
  title and text, and in its context the repository's open follow-up
  issues (`[landing] followup_label`, newest first, at most ten, each as
  `- title (url)`). It then advances like any `auto` plan;
- a `plan.propose` operation whose `target_kind` is `plan`, `target_key`
  the plan's id and `actor` the planner;
- a `GET /v1/decisions` row with `action: "plan.propose"`: `allow` naming
  the plan, its root node and the operation, with facts `repository`,
  `level` (the root's: `initiative` when the goal's text is 1200 characters
  or more, `epic` otherwise), `goal_id`, `chain_depth` and `followups` (the
  issue numbers in the brief); or an `escalate` with no plan, the facts
  naming the `goal_id` — no grant, a grant that falls short, a repository
  that is not configured, is disabled or cannot hold a plan, a follow-up
  listing that could not be read. It is written once while the situation
  stands, closed `acted` when the goal has an open plan again and
  `superseded` when the goal is deleted or no longer `active`.

A follow-up filed by a run of an agent-proposed plan carries an origin
marker (`<!-- lantern:origin item=none agent=planner depth=N -->`) after its
`lantern-followup` marker, and the proposer never reads one at or beyond
`[agent_team] max_chain_depth` into a brief. A goal is never set `done` by
the daemon: it stays `active` until an owner changes it.

Every mutation names the plan's `revision` it read as `expected_revision`;
any write to the plan or any node bumps it, and a stale one is `409 stale_revision` with `current_revision`. An unknown repository is `422 unknown_repository`; one whose forge cannot hold a plan is `409 planning_unsupported` with the reason. Each entry of `GET /v1/repositories`
says that before anyone types: `planning: {hierarchy, reason}`, where
`hierarchy` is `native` (GitHub sub-issues), `checklist` (GitLab: level
labels and a managed checklist in the parent) or `unsupported` (Gitea: "this
repository's forge can't hold plans: Gitea is not supported"; or `[planning] enabled = false` for it: "planning is off for this repository"). Changes emit
`plan.created` `{plan_id, level, repository}` and `plan.node.changed`
`{plan_id, node_id, change}` (`added`, `updated`, `removed`, `archived`,
`deleted`, `advance` with `advance` and `before`, `approved` with
`node_ids`, `published` with `number`,
`issue_edited`, `attached` and `detached` — see below; a re-plan's `closed`
with `number` and `replan_discarded` with `entry_ids`; and `closed`,
`reopened` or `completed` with `number` — see "Closing what is finished").

**Approving and publishing a level (#2341).** `approve` (`plans:create`) is
a person's "this is right": the node's `draft` and `proposed` children —
every one, or those `node_ids` names (a name that is not a child is `422`) —
become `approved`; with none left to approve it is `422`. Approving is an
operation, as publishing is: each call is recorded as `plan.approve` under
whoever made it (`GET /v1/operations?target_kind=plan&target_id=<plan id>`,
with the plan, the node, the revision and the `node_ids` asked for), and
its `operation.accepted` and `operation.finished` ride the chronology. The
answer on success is the plan, as it always was. A refusal — a stale
revision, a name that is not a child, nothing to approve — finishes the
operation `failed` and answers the status, `code` and fields it always did,
plus `operation_id`; with no operation record to write to it is `503 daemon_not_ready` and nothing is approved. The `Idempotency-Key` header is
optional: with one, a replay answers the plan as it is now (or the refusal
the first call recorded) and approves nothing again, and a different body
under the same key is `409 idempotency_conflict`; without one each call is
its own operation. An approve the daemon died during is settled at the next
start from the plan itself: `succeeded` when the plan was written to since
and the children it named are no longer `draft` or `proposed`, `failed`
(`interrupted_before_effect`) otherwise — approving again is safe. `publish`
(`plans:publish`) writes one level to the forge: the node's `approved`
children, and the node itself first when it is not on the forge yet (the
root of a fresh plan). Children that are not approved are not published.
Each node, in dependency order, is looked for by its marker
`<!-- sbx-plan: <plan_id>/<node_id> -->` among the repository's issues
carrying its level label (open or closed, every page), created when absent
with its sections rendered as markdown headings (Goal, Context, Acceptance
criteria as a checkbox list, Kind, Verify commands as a code block, Depends
on as issue references, Non-goals, Constraints) and the marker at the foot,
with its level label (`sbx:initiative`, `sbx:epic`, `sbx:task`) on the
create itself — never the trigger or the workload label, so a published
task is inert until an epic run admits it or a person labels it. It is linked under its parent: a
native sub-issue on GitHub (one already linked is not linked twice), a line
in the parent's managed checklist on GitLab. A cross-repository sub-issue
GitHub refuses falls back to the checklist, and the node's result names why
in `reason` (**field-unverified**: whether a GitHub App installation that
does not cover both repositories refuses). Then the node is recorded
`published` with `forge: {number, url, state}` — one write per node, so a
walk that dies part-way resumes where it stopped.

Each result is `{node_id, outcome, number?, url?, linked, error?, reason?}`:
`outcome` is `created`, `found` (an earlier, interrupted attempt created
it) or `failed` with the forge's words in `error`; `linked` is `native`,
`checklist` or `none` (the root). A failed node stays as it was, and the
nodes under it or depending on it are reported failed without being
attempted; repeating the call resumes and duplicates nothing. The level is
refused before the forge is touched when the node is a task (`422`), is
not the root and not yet on the forge (`409 parent_unpublished`), has no
approved children left and is already published (`409 nothing_to_publish`),
would hold more children than `[planning] max_epics_per_initiative` or
`max_tasks_per_epic` (`409 too_many_children` with `cap`), or has a child
depending on a sibling that is neither published nor approved (`409 dependency_unpublished` naming them, with `node_ids`); when a repository
involved is disabled (`409 repository_disabled`), cannot hold a plan (`409 planning_unsupported` with the reason — Gitea), or lives on another forge
than the daemon's connection (`409 forge_mismatch`); and with no forge
connection at all (`503 source_unavailable`). One plan publishes one level
at a time (`409 already_in_progress`). The `Idempotency-Key` header is
required (`422 idempotency_key_required`): a replay answers the recorded
results with the plan as it is now and `replayed: true`, or the refusal it
recorded; a different body under the same key is `409 idempotency_conflict`. A publish the daemon died during is settled `failed`
(`interrupted_before_effect`) at the next start; publishing again resumes
it. Each call records `plan.published` `{plan_id, node_id, published, failed}` (node ids) with its result.

**The forge wins after publish (#2342).** Lantern re-reads a published
plan's tree from the forge and folds it in; it never writes to the forge
doing so, so a person's edit there is never overwritten. `GET /v1/plans/{id}` does it first when the plan has anything on the forge and its
last reading is older than `[planning] reconcile_interval_s` (default 120
seconds; `0` only on sync), and never fails for the forge: a forge that is
down, or a daemon with no forge connection, serves the stored plan with
`reconciled_at` (the last reading that succeeded) and the reason in
`reconcile_error`. A read never boots an idle forge sandbox; it says so
and a sync does. `POST /v1/plans/{id}/sync` (`plans:create`: it writes
the plan record and spends the forge's rate limit on demand, where a
read's reconcile is paced) does it now: `503 source_unavailable` when the
forge cannot be reached at all, issues it would not answer named in
`reconcile_error`, `409 plan_archived` for an archived plan and `409 already_in_progress` while the plan is published or read. Per published
node, the forge's title, the sections under the rendered headings (text
outside them is a person's and is not read; a ticked criterion is the same
criterion; `Depends on` references become sibling ids) and the issue's
state (`open`/`closed` in `forge.state`) update the node, with the
issue's `updated_at` as `forge.updated_at`; a child a person
added — a sub-issue or checklist line whose issue carries no marker of this
plan, or one naming a node the plan no longer has — is adopted one level
down as `published` with `origin: forge`, its sections read from its body
where it has our headings and its whole text as the goal where it has
none; a known node listed under another parent of the right level is
moved; a node its parent no longer lists, or whose issue is gone (GitHub
410/404, GitLab 404), is detached — `forge.detached` names why, it is not
followed any more (its subtree is left as it was) and nothing is
recreated; one listed again is attached again. A removed marker sets
`forge.marker_missing`; a managed checklist a person broke sets
`forge.checklist_error` on the parent and none of its children is judged;
neither is repaired. A GitLab checklist tick is not written by a reconcile.
A sub-issue under a task is not part of the plan (a task is one run), and
the forge's order of children is not read.

Each change is recorded on its node as `drift`, `[{change, at, before, after, reason}]`, until someone marks it seen: `title`, `sections` and
`state` carry `before` (as a person last saw it) and `after` (as the forge
has it), keyed by field, and a second edit before anyone looks moves
`after` and keeps `before` (an edit back to what was seen clears it);
`adopted`, `moved` and `reattached` carry the parent; `detached`,
`marker_removed` and `checklist_mangled` carry `reason`. A plan's `drift`
counts its nodes with unseen drift (the badge). Each change is also a
`plan.drift` event `{plan_id, node_id, change, …}` (`before`/`after` for
`title` and `state`, `fields` for `sections`, `parent_id` and `number` for
`adopted`, `reason` for the reported kinds), in the same transaction as the
fold; a reading that changes nothing bumps no revision. `POST .../drift/ack`
(`plans:create`) marks the drift of every node, or of those `node_ids`
names, seen (`422` for a name not in the plan; nothing to mark changes
nothing) and records `plan.node.changed` with `change: drift_seen` and
`node_ids`. **field-unverified**: that GitHub's sub-issue listing carries
each child's body (the reconcile reads the issue when it does not), and
that a GitHub issue this server's credential can no longer see answers
404 like a deleted one — such a node is detached, and attached again once
it is listed again.

**From chat.** The concierge's `draft_plan` tool drafts a plan through the
same service, on the asking person's yes and with their `plans:create`: the
plan's `created_by` is their id, and `plan.created` names them as the actor
with `via: "concierge"`. It writes only the draft. Its reply links the draft
as the relative path `/plans/<plan_id>` (the id URL-encoded), which a client
opens as its plan screen: Lantern serves the page at that path on its own
origin, and Lantern opens the same path from a message.

**Breakdown.** `POST /v1/plans/{id}/nodes/{node_id}/breakdown`
(`plans:create`) asks the planner for the node's next level — an
initiative's epics or an epic's tasks. It queues a run of the fourth run
kind, `plan`, admitted like any work (an `item.admit` operation; an
optional `Idempotency-Key`): the item is `kind: "plan"`, it appears in the
queue and History, can be cancelled, and answers to `channel_id` when one is
named (checked as any channel-linked admission is), where its chronology is
told and a person can steer it. The run reads a read-only checkout of the
node's repository cut on the host into its data directory (other
repositories a kept epic targets are named to the planner, not checked
out); it holds no write credential and never touches the forge. The planner proposes at most the
level's cap (`[planning] max_epics_per_initiative` / `max_tasks_per_epic`,
less the children that stay), each child with the sections above; a task
carries `kind`, acceptance criteria, `verify_commands` for code and a
configured `workload_profile` for workload, and `depends_on` among its
siblings. An answer that breaks a rule is sent back once, the way the
in-run decompose is; a second is a failed generation. The proposal is
delivered to the plan, never to the forge: it replaces the node's previous
`proposed` children (and anything under them), leaves the children a person
made or approved where they are, and adds each proposed child as `proposed`
with `origin: "planner"` and its dependencies mapped to the new ids — one
write, one revision. Refused: a task (`422`, it has no children); a
repository planning is off for or no longer configured (`409 planning_unsupported`, `409 unknown_repository`); a level at its cap (`409 level_full`); a breakdown of the node already queued or running (`409 already_in_progress`, `plan_code: generation_in_progress`); a stale
`expected_revision`. The generation emits `plan.generation.started`
`{plan_id, node_id, run_id}` when the run starts, then
`plan.generation.proposed` `{plan_id, node_id, run_id, kind: "breakdown", count}` when the plan holds the proposal, or `plan.generation.failed`
`{plan_id, node_id, run_id, reason}` when the run ends without one; each is
scoped to the run, its item and its channel.

**A reviewed breakdown.** On a plan whose `advance` is `auto` the planner
asks no clarifying questions, whatever `[planning] max_questions` says, and
the run's critic reviews the proposal before it is delivered: it reads the
node (or the root the planner generated), the children that stay and the
proposed children as their issues will read, and answers `approve` or
`escalate` with short reasons. The verdict is written to the node's
`review` in the same write as the proposal, and
`plan.generation.reviewed` `{plan_id, node_id, run_id, verdict, reason_count}` follows `plan.generation.proposed` in that write, scoped
the same way (the reasons themselves are on the node, not in the event).
A reviewer whose answer is unusable twice stands for `escalate`; the
proposal is still delivered and waits for a person. A re-plan is not
reviewed: its diff already waits for a person.

**Clarifying questions** (feature `planning.clarify`, advertised with
`planning`). Before it proposes, the planner reads the checkout and either
says it is ready or asks up to `[planning] max_questions` questions (the
repository's own under `[vcs.repos.planning]`; `0` never asks). Each
question has the chat choice question's shape: `{id, prompt, choices: [{value, label, description?}], allow_free_text}`, two to five choices and
free text unless `allow_free_text` is false. The questions are written to
the node's `generation` — `{run_id, status, questions, answers, asked_at, answered_at?, answered_by?}`, `status` `awaiting_answers` while the run
waits, then `answered`, `skipped`, or `withdrawn` when the item was
abandoned first — and emitted as `plan.generation.questions` `{plan_id, node_id, run_id, questions}`, scoped to the run, its item and its channel.
The run parks `awaiting_answers` (its item too): no sandbox is kept and
nothing is spent while it waits, and it survives a daemon restart.
`POST /v1/plans/{id}/nodes/{node_id}/answers` (`plans:create`) answers
them, keyed by question id — a choice's `value`, `text` in the person's own
words where the question allows it, or both — or `{"skip": true}` lets the
planner decide; a question left out goes to the planner unanswered. The
answers are recorded on the node, `plan.generation.answered` `{plan_id, node_id, run_id, skipped, answers}` is emitted, and the run goes back to
the queue: it resumes without asking again and proposes with the answers
in its prompt. The same questions can be answered from the run's chat
thread (a click on a choice, or a reply), one question at a time; the run
resumes once every one has an answer. Refused: nothing waiting on the node
(`409 no_questions`); questions already answered, skipped or withdrawn
(`409 already_answered`); the run that asked them no longer waiting (`409 not_awaiting_answers`); an unknown question id, a value that is not one of
the question's choices, text for a question that takes only its choices,
answers together with `skip`, or neither (`422`); a stale
`expected_revision` (optional here).

**Editing a published plan, attaching and detaching (#2350).** After
publish Lantern writes to a plan's issues only when a person asks, and
never over a change made on the forge. Each of these needs `plans:publish`
and writes the forge at once; a daemon with no forge connection is `503 source_unavailable`, a forge that refuses a write `502 forge_refused` with its
words, and a plan being published, read from the forge or written right
now `409 already_in_progress`.

`PATCH /v1/plans/{id}/nodes/{node_id}` with sections on a published node
writes them to its issue (`plans:create` alone is `403 forbidden` naming
`plans:publish`; moving a published node among its siblings stays
`plans:create`). The request names `forge_version`, the version of the
issue the client read: the node's `forge.version` — a digest of its title
and sections as Lantern last read or wrote them — or the one a refusal
answered with (a missing one is `422`). The issue is read first, the way a
reconcile reads it; if its title or sections changed on the forge since,
the edit is `409 forge_changed` with the forge's `forge_version` and
`current: {title, goal, …, forge_version, number, url}` (its title and
sections as read), and nothing is written. Editing again naming that
version applies the edit to the forge's version: sections the person did
not touch keep the forge's text. The write sends the title only when it
changed and rewrites in the body only the sections the edit changed — a
person's text outside the rendered headings, the other sections (a ticked
criterion stays ticked), the marker and the managed checklist keep their
text; an issue adopted from the forge whose whole text was read as its
goal is rewritten as headings, the goal first. A dependency the edit adds
must be a sibling that follows its issue (`409 dependency_unpublished`); a
detached node is `409 node_detached`, an issue gone from the forge `409 issue_gone`, and a stale `expected_revision` `409 stale_revision` as
anywhere. The version is a content digest rather than the forge's
`updated_at` because a comment, a label or lantern's own checklist and
sub-issue writes move `updated_at` without touching what an edit
overwrites. A limit: neither forge offers a conditional issue update, so
a forge edit landing between lantern's read and its write — one request
apart — is not seen and is overwritten (Lantern itself holds one write
per plan at a time; the window against a person on the forge is not
exercised against a real forge, **field-unverified**). Records
`plan.node.changed` with `change: issue_edited`, `number`, `fields` (the
node fields that changed) and `wrote` (`title`, `body`).

`POST .../nodes/{node_id}/attach` links an existing open issue — named by
`repository` and `number`, or by its web `url` — as a child one level
under the node, which must follow its issue: a native sub-issue on GitHub,
a line in the parent's managed checklist on GitLab (or, like publishing,
where GitHub refuses a cross-repository sub-issue, with the reason), then
its level label — never the trigger or the workload label. It is recorded
`published` with `origin: forge`, its sections read from its body the way
a reconcile adopts an issue (its whole text as the goal where it has none
of our headings), and the answer is `{plan, node_id, linked, reason}` with
`Location` naming the node. A node of this plan detached from the same
issue follows it again instead of a new one. Refused before anything is
linked: a closed issue (`409 issue_closed`), a pull request (`422 not_an_issue`), no such issue (`404 issue_not_found`), an issue already in
this plan (`409 already_in_plan` with `node_id`) or carrying another
plan's marker (`409 in_another_plan`), a task outside its epic's
repository or a task as the parent (`422`), a parent at its cap (`409 too_many_children`), a broken managed checklist (`409 checklist_mangled`),
and on GitHub an issue already under another parent (GitHub's 422, `409 already_has_parent`: moving it is a person's decision there). On GitLab an
issue listed in another parent's checklist carries no link Lantern can
see and is not refused. Records `change: attached` with `parent_id`,
`number`, `url`, `linked` and `reattached`.

`POST .../nodes/{node_id}/detach` unlinks a published child from its
parent without closing its issue: the GitHub sub-issue link and any line
in the parent's checklist go. The node stays in the plan detached, as a
reconcile leaves a node its parent no longer lists: `forge.detached` says
a person detached it, it is no longer followed (its subtree is left as it
was), and linking its issue again — attaching it, or on the forge — makes
it followed again. Its siblings stop depending on it, in the plan and in
their issues: only the `Depends on` items naming it are removed. The root
is `422` (archive the plan instead), an unpublished node `409 node_unpublished` (remove it instead) and a detached one `409 node_detached`. Records `change: detached` with `parent_id`, `number`,
`unlinked` (`native`, `checklist`) and `dependents`.

lantern's own writes are what the next reconcile reads back: an edit,
attach or detach never shows as drift. **field-unverified**: GitHub's 422
for a sub-issue that already has a parent is the documented answer; the
exact status GitHub gives a cross-repository refusal is not, and any other
refusal of a cross-repository link falls back to the checklist.

**Re-plan (#2346).** A breakdown of a node that is on the forge with at
least one child followed there is a re-plan: the item is titled "Re-plan
the tasks of …" and the run proposes a diff against the node's current
children, never a replacement (a published node whose children are all
still drafts or proposals is broken down as before). When the run starts
proposing, the daemon reconciles the plan from the forge first — reading
only — so the planner is given every current child as the forge has it
now, by node id, with its issue, its state and its sections; a forge that
cannot be read fails the generation named ("the forge could not be read
before re-planning: …"). The answer is `{add, modify, suggest_close}`:
`add` whole children (at most the level's cap less the children it has),
`modify` a current child by id with only the sections that change,
`suggest_close` a current child by id with a rationale; every entry says
why. It is held to the level's rules with one retry, as a breakdown is, and
an addition whose title or id is a current child's is sent back — a child
that exists is never proposed twice. Only an open, followed child can be
changed or closed, and a child a person filed on the forge in their own
words (`origin: forge`) can be closed but not rewritten. A full level does
not refuse a re-plan.

The diff is kept on the node as `replan` `{id, run_id, proposed_at, entries}` and nothing else changes: each entry is `{id, action, node_id, sections, before, rationale, error, forge_version}` — for `add`, `node_id` is the id the
child will have (minted now, so its issue's marker is the same on every
attempt) and `sections` the whole child with `depends_on` as node ids; for
`modify`, `sections` holds only what changes and `before` those sections as
the child had them, and for `modify` and `suggest_close` `forge_version`
is the child's issue version then; `error` is why the last approval of the
entry failed.
A new re-plan replaces the diff waiting; an empty one clears it. Delivery
leaves out an addition that repeats a child the node has meanwhile and an
entry whose child left the forge. It is recorded as
`plan.generation.proposed` `{plan_id, node_id, run_id, kind: "replan", replan_id, count, add, modify, suggest_close, skipped}`.

`POST .../replan/approve` (`plans:publish`) applies every entry, or those
`entry_ids` names, through the publish path. The plan is reconciled from the forge
first. Each `modify` is written the way a person's direct edit of a
published node is (`PATCH .../nodes/{node_id}` above): the diff recorded
the child's `forge.version` when it was proposed, and the issue is read and
refused when it no longer reads so — a child a person changed on the forge
since fails naming what moved, and nothing is written; otherwise only the
title (when it changes) and the sections that change are rewritten, so a
person's own text, the sections not changed, the marker and a managed
checklist are kept, and it is recorded as `plan.node.changed` `change: issue_edited` with `via: "replan"`. Each
`suggest_close` closes the issue as not planned (GitHub's `state_reason`;
GitLab records no reason), then comments the rationale and who approved it;
the node stays in the plan, closed, and an issue already closed is left
alone. Each `add` becomes an `approved` child with its minted id and is
published by the level walk narrowed to the additions: looked for by its
marker first (an interrupted approval's issue is `found`, never filed
twice), created with its level label, linked as a sub-issue or checklist
line; an addition whose title a child now has is refused. Each result is
`{entry_id, action, outcome, node_id, number?, url?, error?, reason?}` with
`outcome` `created`, `found`, `updated`, `closed` or `failed`. An entry that
lands leaves the diff; one that fails stays with its `error`. Refused before
the forge is touched: no diff waiting (`409 no_replan`), an entry not in it
(`422`), a disabled or unplannable repository, additions past the cap (`409 too_many_children`), an addition depending on one not approved with it
(`409 dependency_unpublished`), no forge connection (`503 source_unavailable`), a plan being published (`409 already_in_progress`). The
`Idempotency-Key` behaves as publish's; an approval the daemon died during
is settled `failed` (`interrupted_before_effect`) and approving again
resumes it. Each call records `plan.published` `{plan_id, node_id, replan: true, published, modified, closed, failed}` (node ids) and a
`plan.node.changed` per child written (`added`, `issue_edited` or `closed`,
with `via: "replan"`). `POST .../replan/discard` (`plans:create`) drops entries and writes
nothing to the forge (`plan.node.changed` `change: replan_discarded` with
`entry_ids`).

**Running an epic (#2347).** `run` (`plans:publish`, advertised as
`planning.run`) starts an **epic run** on a published epic whose tasks are
on the forge; the daemon owns it from there. Every task whose `depends_on`
are all closed is admitted at once through the same issue admission as
`POST /v1/items` — the same rules (open, an issue, not in progress, not
queued for the other kind) — but never by applying the trigger or the
workload label, so no poll-driven path is added, and with the item's
`parent_item_id` naming the epic run. A code task is admitted as a code run;
a workload task as a workload run under its `workload_profile`, the same as
the workload-label path. Independent tasks are queued together and run as
the queue, the holds and the usage pool allow, exactly as any other item.
The claim puts the in-progress label on as usual; a code run that lands
closes its issue through its pull request's `Closes` and the merge report,
a workload run that delivers closes it with its completed report, and
either makes its dependents ready on the daemon's next pass. A task whose
run fails (after its attempts), is blocked or is cancelled is `failed` with
the reason, and only its dependents — directly or through another task —
are `blocked` (the reason names which dependency) and not admitted; every
other task goes on. When every task is landed, closed or skipped the run is
`completed`.

The answer (and `GET .../run`) is `{id, plan_id, node_id, state, started_by, started_by_display, created_at, updated_at, completed_at, tasks}` (`erun_…`,
`state` `running`, `paused`, `completed` or `cancelled`), each task
`{node_id, title, kind, workload_profile, depends_on, forge, state, item_id, run_id, reason, admitted_at, updated_at}` with `state` `waiting` (a
dependency is not closed), `ready` (the run is paused, or the forge could
not be read; tried again next pass), `queued`, `running` (a merge gate or
review wait included), `landed`, `closed` (its issue was already closed),
`failed`, `blocked` (by a dependency, named in `reason`), `skipped` (a
person treated it as done) or `cancelled` (the run was stopped before it
ran). `item_id` and `run_id` link a task to its item
and its run's thread. A task already queued (a person started it alone) is
adopted rather than admitted twice. The start is refused when the node is
not an epic (`422`), the epic is not on the forge (`409 epic_unpublished`),
none of its tasks is (`409 nothing_to_run`), it is already running (`409 already_running` with `epic_run_id`), its repository is unknown or disabled
(`422 unknown_repository`, `409 repository_disabled`), the daemon polls no
repository (`503 source_unavailable`), or on a stale revision (`409 stale_revision`). The `Idempotency-Key` header is required: a replay answers
the run as it is now with `replayed: true`, or the refusal it recorded; a
different body under the same key is `409 idempotency_conflict`. A start
the daemon died during is settled at the next start from the record: the
run is there (`succeeded`) or it never started (`failed`). It records
`plan.run.started` `{plan_id, node_id, epic_run_id}`, one
`plan.run.task_admitted` per task admitted, and `plan.run.completed` `{plan_id, node_id, epic_run_id, landed, closed, skipped}` (task node ids).

**Controlling an epic run (#2348).** Five routes steer a run, each
`plans:publish` with the `Idempotency-Key` header required (a replay
answers the run as it is now with `replayed: true`, or the refusal it
recorded) and each answering `200` with the run and its `operation_id`:

- `.../nodes/{epic_id}/run/pause` stops admission: the run is `paused`,
  nothing new is admitted, and tasks already queued or running are **not**
  cancelled — they go on, and the run still follows them (a ready task
  shows `ready`). `409 already_paused`.
- `.../run/resume` sets it `running` again and admits the ready set at
  once. `409 not_paused`.
- `.../run/cancel` stops it for good: the run is `cancelled`; a task not
  yet admitted never is (`cancelled`); a task whose item is still waiting
  in the queue (no run started) has that item withdrawn through the item
  abandon (`cancelled`, the item `failed`; an issue that was never claimed
  is not written to); a task whose run is under way is **not** killed — it
  finishes and the run keeps following it until it settles. Cancel that
  run with `POST /v1/runs/{run_id}/cancel` (`runs:control`) if it should
  stop too.
- `.../nodes/{task_id}/run/retry` runs a `failed` task again, in the latest
  epic run that holds it — allowed while the run is paused. An item that
  failed, was blocked or was cancelled is re-queued through the item retry
  (`POST /v1/items/{id}/retry`'s path: attempts start over, a fresh run,
  the issue's failed or blocked label cleared and a "re-queued by" comment);
  a task whose admission was refused is admitted afresh. Its dependents go
  back to `waiting`. `409 task_blocked` (with `blocked_by`: retry or skip
  that task first) or `409 task_not_failed`.
- `.../run/skip` treats a task that is not under way — `failed`, `blocked`,
  `waiting` or `ready` — as done: it is `skipped` and its dependents become
  ready. Its issue is left exactly as it is (Lantern does not close it) and
  its item is not touched. `409 task_in_progress` (queued or running: let it
  finish or abandon its item) or `409 task_settled`.

All five are `409 run_ended` (with `epic_run_id`, `state`) on a completed
or cancelled run, `404` when the node never ran, and `422` when an epic's
control names a task or a task's names an epic. A control the daemon died
during is settled at the next start from the run's or the task's state.

Every move of a task records one event `{plan_id, node_id, epic_run_id, task_node_id, from, state, item_id, run_id, reason}`: `plan.run.task_admitted`
when it becomes an item, `plan.run.task_retried` when a failed task's item
is re-queued (with `via`: `item` or `admission`, and `by` from the retry
route), `plan.run.task_skipped` (with `by`), and otherwise
`plan.run.task_<state>` — `task_running`, `task_landed`, `task_closed`,
`task_failed`, `task_blocked` (with `blocked_by`), `task_waiting`,
`task_ready`, `task_queued`, `task_cancelled`. The run's own moves record
`plan.run.paused`, `plan.run.resumed` `{…, by}`, `plan.run.cancelled`
`{…, by, withdrawn, running}` (task node ids) and `plan.run.completed`.
`plan.run.paused` means **the run needs a person**: `reason: "person"`
(with `by`) when someone paused it, and `reason: "task_failed"` —
`{task_node_id, item_id, error, blocked, state}`, `blocked` the dependents
now held back — each time a task fails in a running or paused run. A
failure does not change the run's own `state` (still `running`: every
task that does not depend on the failed one goes on), but the run cannot
complete until that task is retried or skipped, which is what the notice
is for.

The issue of a task an epic run admitted is worded around its plan: its
claim comment names the epic run, and the abandon, blocked and cancel
comments say to retry or skip the task from its plan rather than to
re-add the trigger label.

**Closing what is finished (#2349).** After publish the forge is the
record, so an epic is finished when the issue of **every one of its
published tasks is closed on the forge**, and an initiative when every one
of its published epics is. A task an epic run skipped is done for the run
(its dependents go ahead and the run can complete) but its issue stays
open, and an open task keeps its epic open: closing that issue later
finishes the epic then. With `[planning] close_completed` on for the
node's repository (the default), Lantern comments a summary on a finished
epic — each task as `landed`, `closed` or `skipped in the epic run, then closed on the forge`, with its pull request or delivery link where
known, and the epic run and who started it — and closes it as completed;
the last epic of an initiative closing does the same for the initiative
with a rollup of its epics. Each summary starts with a hidden
`<!-- sbx-plan-summary: <plan_id>/<node_id> -->` marker, so an attempt that
died between comment and close never comments twice, and an issue a person
already closed gets no comment. With `close_completed = false` both stay
open. Whatever `close_completed` says, a parent's managed checklist
(GitLab, or GitHub's cross-repository fallback) has each child's line
ticked as its issue closes, and each node whose issue changed state is
recorded in `forge.state` with `plan.node.changed` — `change: "closed"`
(or `"reopened"`) for a state read from the forge, `"completed"` for a
node Lantern closed with a summary, each with `number`. The daemon looks
when a pass of an epic run sees a task land or close and when the run
completes; every 10 minutes, for 14 days after a run completed, while its
epic is still open; and when a merge or delivery report closes the issue
of a plan's task outside any live epic run (a person started the task
alone). A forge that cannot be reached is logged and looked at again.

### Workspace people

A workspace holds owners, admins and members. These routes are advertised as
`users.directory` and `workspace.members`:

| Route                                    | Who          | Result                                                             |
| ---------------------------------------- | ------------ | ------------------------------------------------------------------ |
| `GET /v1/users`                          | any member   | `{data: [user]}`, oldest member first                              |
| `PATCH /v1/workspace/members/{user_id}`  | admin, owner | `{role?, is_active?}`, answers the updated `user`                  |
| `DELETE /v1/workspace/members/{user_id}` | admin, owner | `204`; the membership ends                                         |
| `POST /v1/workspace/invites`             | admin, owner | `201 {id, token, expires_at, role, email}`                         |
| `GET /v1/workspace/invites`              | admin, owner | `{data: [{id, role, email, expires_at, accepted_at, created_by}]}` |
| `DELETE /v1/workspace/invites/{id}`      | admin, owner | `204`; the invite's token admits nobody                            |

A `user` is `{id, username, email, full_name, avatar_url, role, is_active, auth_source, last_seen_at}`, where `role` is `owner`, `admin` or `member`,
`auth_source` is `local` or `oidc`, and `last_seen_at` is the last
authenticated request (recorded at most once a minute) or `null`.
`GET /v1/users/me` also carries the caller's `role`, `avatar_url` and
`auth_source`.

Rules:

- Only an owner grants the owner role, invites an owner, or changes,
  deactivates or removes an owner (`403 owner_required`).
- Nobody deactivates or removes themselves (`409 self_action`).
- The workspace always keeps an active owner (`409 last_owner`).
- A caller below admin is refused with `403 forbidden_role`. A plain API
  client with no user counts as an owner when it holds `daemon:manage`, and
  is refused with `forbidden_role` otherwise.
- An unknown user or invite is `404 user_not_found` or `404 invite_not_found`.
  An invite already spent cannot be revoked (`409 invite_accepted`).

Deactivating a user (`is_active: false`) revokes their refresh tokens, and
every access token they hold is refused at once (`401 user_inactive`), as is
their login. Reactivating restores their role's capabilities. Removing a
member leaves their client with no capability and revokes its refresh
tokens. The refresh tokens are revoked in the same database transaction as
the membership change, so either both happen or neither does.

A removed local user can rejoin without creating a duplicate account. After
an admin or owner creates a fresh invite, the user sends their username,
password and `invite_token` to `POST /v1/auth/local/login`. The password is
checked before the invite is spent; an email-addressed invite must match the
account's email. The response contains new access and refresh tokens with
the invite's role. A deactivated user cannot rejoin this way.

Removing or deactivating a member also ends every standing they had:

- They are taken out of every channel. A private channel they were in stays
  hidden after a later invite until someone adds them again. Where they were
  a channel's last owner, its longest-standing member still in the workspace
  becomes the owner; a channel nobody else was in is left without a member.
- The invites they created and nobody had used yet are withdrawn. Demoting a
  member withdraws the unused invites they created above the new role. Each
  withdrawal is a `workspace.invite.revoked` event whose `reason` is
  `creator_removed`, `creator_deactivated` or `creator_demoted`.

An invite stands only while its creator does: one whose creator is no longer
an active member admits nobody (`403 invite_invalid`). Demoting its creator
withdraws any pending invite above the new role. An invite a plain operator
client created (`created_by` of `client:<id>`) is not measured against a
membership.

An invite's token appears only in the creation response; the daemon keeps
its SHA-256. `ttl_hours` defaults to 72 and may be 1 to 720. An invite with
an `email` admits only a registration with that email, compared without
regard to case (`403 invite_email_mismatch`). The `email` is trimmed of
surrounding whitespace first; an empty or all-whitespace `email` is treated
as absent, so that invite admits any address. An invite's `created_by`, and
the `invited_by` of the membership it creates, is the inviting user's id, or
`client:<client id>` when a plain operator client created it.

Every change records an event without any token:
`workspace.member.updated`, `workspace.member.removed`,
`workspace.invite.created` and `workspace.invite.revoked`, each with the
acting user or client as `actor`.

### Channel access, members and participants

A channel is `private` (its channel members only) or `workspace` (every
workspace member). Channels list and read with `visibility`, `created_by`,
`silenced_until` and the caller's `my_role` (`owner`, `member`, or null when
the caller has not joined). `GET /v1/channels` lists the channels the caller
belongs to plus every workspace channel. The rules:

- A private channel the caller does not belong to answers `404 channel_not_found` on every route, exactly like an unknown id, whatever the
  caller's workspace role.
- Any workspace member may read and post to a workspace channel. Posting (a
  turn, a reaction, a participant change) makes the caller a channel member.
- Managing a channel (`PATCH` title or `visibility`, `DELETE`, adding or
  removing someone else) takes the channel's owner, or a workspace owner or
  admin who can see it; anyone else gets `403 channel_forbidden`.
- A turn may be cancelled by the person who asked or by someone who manages
  the channel.
- Teams and preferences stay per person.

When `/v1/capabilities` lists `collaboration.channel_members`:

| Route                                        | Needs  | Result                                                                                                                       |
| -------------------------------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `GET /v1/channels/{id}/members`              | read   | `{data: [{user_id, role, joined_at, last_read_sequence, user: {id, username, full_name, avatar_url}}]}`                      |
| `POST /v1/channels/{id}/members`             | manage | Body `{user_id, role?}` (`member` by default); `201` with the entry; `200` when a role changed; `409 already_channel_member` |
| `DELETE /v1/channels/{id}/members/{user_id}` | manage | `204`; one's own id leaves the channel and needs only read                                                                   |

The user must be an active workspace member (`404 user_not_found`). Posting a
current member with an explicit `role` other than theirs changes that role in
place and answers `200`; with no `role`, or the role they already have, it is
`409 already_channel_member`. The last channel owner cannot step down (`409 last_channel_owner`), nor leave or be removed while anyone else remains: make
another member an owner first. Only channel members still active in the
workspace count, as another owner or as someone left behind. Changes record `collaboration.member.added`,
`collaboration.member.updated` and `collaboration.member.removed` events with
`{channel_id, user_id}`.

When `/v1/capabilities` lists `collaboration.participants`, agents are channel
participants:

| Route                                          | Needs | Result                                                                                         |
| ---------------------------------------------- | ----- | ---------------------------------------------------------------------------------------------- |
| `GET /v1/channels/{id}/participants`           | read  | `{data: [{agent_slug, mode, added_by, muted_until, created_at, status, activity}]}`            |
| `PUT /v1/channels/{id}/participants/{slug}`    | post  | Body `{mode?, muted_until?}`; adds the agent (`mention` by default) or changes the fields sent |
| `DELETE /v1/channels/{id}/participants/{slug}` | post  | `204`; `404 participant_not_found` when it is not in the channel                               |

`slug` must name an enabled agent in the registry (`404 agent_not_found`).
`added_by` is an author object. `status` is `thinking` while the agent answers
a running turn in the channel, `working` while a live run linked to the
channel is credited to it (`activity` is then the run's title), and `idle`
otherwise. Mentioning an agent with `@slug`, or targeting it, adds it as a
`mention` participant when the turn is accepted. Changes record
`collaboration.participant.added`, `.updated` and `.removed` with
`{channel_id, agent_slug}`; an agent starting and finishing its part of a
turn records `collaboration.participant.activity` with `{channel_id, agent_slug, status}` (`thinking`, then `idle`; Lantern reports as `concierge`).

An agent's reply is prose in the channel, so `@slug` in it addresses that
agent: a follow-up turn is accepted for it, with `trigger: "mention"`, the
replying agent as its author, the reply as its input message,
`parent_turn_id` naming the turn that produced the reply, and `chain_depth`
one deeper. Mentions inside a fenced or inline code span and inside a block
quote address nobody, an agent never addresses itself, and at most four
agents are addressed from one reply. The agent joins the channel as a
`mention` participant if it is not one already. An agent still to answer
in the same turn, whether the person asked for it or a peer handed off to
it, is not addressed again: it sees the reply in that turn.

A follow-up is a peer request, not the person's. The agent answers the
other agent's message framed as that agent speaking and as no new human
approval, with read-only tools, no MCP servers, no memory writes and no
`handoff_agent`, so one agent's prose cannot make another act on the
person's authority. `handoff_agent` itself, a peer request inside one
turn, is unchanged.

Every follow-up passes the `[collaboration]` guardrails first, and each
decision records `collaboration.followup.queued` or
`collaboration.followup.suppressed` with
`{channel_id, agent_slug, source_agent_slug, trigger, chain_depth, reason, retry_at}`
— never the message text. `reason` is `chain_depth` (past
`max_chain_depth`), `silenced` (the channel is quiet), `channel_rate` or
`agent_rate` (past `channel_turns_per_window` or `agent_turns_per_window`
inside `window_s`), `pair_cooldown` (that agent addressed this one less
than `pair_cooldown_s` ago), or the workspace budget's own reason.

With `[collaboration] ambient = true`, a participant whose `mode` is
`ambient` may also answer a message nobody addressed to it. Every message in
the channel is put through three gates in order, cheapest first: the agent's
`interests` matched case-insensitively over the last `ambient_window_messages`
(no match and no mention means nothing further happens and no model is
called); the guardrails above, with `trigger: "ambient"`, plus
`ambient_max_per_hour` for that agent in that channel; and one short
relevance call on `ambient_model` — the concierge's model when unset — that
answers RELEVANT or PASS. A PASS posts nothing and records
`collaboration.followup.suppressed` with reason `ambient_pass`; being over
the hourly cap records reason `ambient_cap`. Each decision is recorded once,
as its final outcome: `queued` only once the turn exists. What passes all
three becomes a turn with `trigger: "ambient"`, and its reply is an ordinary
agent message; the turn runs read-only with no actions and no handoff, since
nobody asked for it. The relevance call is one-shot and resumes no session.
An agent never answers its own message, an agent already answering the turn
or named in the message does not also volunteer, and each message is looked
at once, by the turn that posted it. `ambient = false`, the default, skips
all of it.

A person has the last word over all of it:

| Route                           | Needs    | Result                                                               |
| ------------------------------- | -------- | -------------------------------------------------------------------- |
| `POST /v1/channels/{id}/stop`   | delegate | `{cancelled_turns, cancelled_runs, cancelled_items, silenced_until}` |
| `POST /v1/channels/{id}/resume` | delegate | The channel, with `silenced_until` cleared                           |
| `PUT /v1/channels/{id}/silence` | delegate | Body `{until}` (a timestamp, or null to lift it); the channel        |
| `PUT /v1/channels/{id}/read`    | write    | Body `{sequence}`; the caller's channel member entry                 |

Stop cancels the channel's queued and running turns (a turn still accepted
that nothing is running included), cancels the runs its
work items are executing, abandons the work items it queued that have not
started, and silences the channel for an hour; resume lifts the silence but
restarts nothing. The runs and items are cancelled through the daemon's
control service with run control scoped to this channel's own work, so a
plain member who may post stops them too, the audit record names that
member, and nothing another channel asked for is touched. Gated work and
work awaiting review is left alone: it already waits on a person, and
dropping it would discard a finished result. A bare `/stop` or `/cancel`
typed in the channel is this same stop (the turn carrying it is left to
answer, and its reply names what was cancelled, abandoned and silenced);
see "Steering and stopping from chat" below.
Silence quiets the agents without cancelling anything. Channel-level
permission for stop, resume and silence is **post**, not manage: a person
watching agents go somewhere they should not is the guard that matters, and
waiting for whoever owns the channel would defeat it. Features:
`collaboration.channel_stop`, `collaboration.silence`.

`PUT /v1/channels/{id}/read` records how far the caller has read. The
sequence only moves forward and never past the newest message, the members
entry carries `last_read_sequence`, and `ConversationOut` gains
`unread_count` (null for a caller with no channel membership, such as a
plain API client). Feature: `collaboration.read_state`.

### Bridge links

A channel can have a window onto a chat service: a Slack, Discord or
Mattermost surface where the same conversation happens. When
`/v1/capabilities` lists `collaboration.bridges`:

| Route                                  | Needs                   | Result                                                                                                              |
| -------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `GET /v1/bridges`                      | read                    | `{data: [{backend, configured, label}]}` — the services this release can bridge, and whether one is set up here     |
| `GET /v1/channels/{id}/links`          | manage                  | `{data: [{id, channel_id, backend, surface_id, thread_id, allow_guests, created_by, created_at, active}]}`          |
| `POST /v1/channels/{id}/links`         | manage, workspace admin | Body `{backend, surface_id, thread_id?, allow_guests?}`; `201` with the link; `409 link_exists` for a taken surface |
| `DELETE /v1/channels/{id}/links/{lid}` | manage                  | `204`; `404 link_not_found`                                                                                         |

Creating a link takes managing the channel and being a workspace owner or
admin (`403 channel_forbidden` otherwise): a link makes the channel hear
everyone on that surface and post its own traffic there, which reaches
past the channel itself. A Discord thread (and the operator console's) is a
channel of its own, so a link given a `thread_id` there is stored with that
thread as its `surface_id` and no `thread_id`; Slack and Mattermost keep
both. Deleting a link and linking the same surface or thread again works.

The thread a bridge opens for a run is such a link for as long as the run is
live, made by the daemon as the run's headline is posted: the link of the
channel the run lives in, admitting guests, so the channel's messages reach
the thread and a reply typed in the thread is a turn in that channel,
steering the run through the same path a reply in the app takes. The daemon
retires the link when the run ends — a channel hosts one run after another,
each with a thread of its own — and makes it again if the run resumes. The bridge keeps rendering what the channel
has no message for — the headline card, the status line and tool digest it
edits in place, the agent's narration — and leaves the plan, the verdicts
and the steering replies to the mirror. With `thread_per_run = false` there
is no thread to link and steering from the bridge stays as it was.

While a surface is linked, what people type there becomes a turn in the
channel it mirrors, instead of reaching the daemon's concierge. A link is a
window on a channel, not a grant of operator powers: it never widens where
`!sbx` runs, so on a linked surface that is not the control channel the one
command is `!sbx link`, and every other is refused with a note saying where
it does run. Commands on the control channel, run-thread steering and an
unlinked surface behave exactly as they did. Every message appended to the
channel — a person's, an agent's, a run's delivery, a failed turn's error,
one agent's request to another — is posted back to each linked surface
under a `**name**` header, except to the link it arrived through, so two
linked services mirror each other without a loop. A message that itself
came in over a bridge is mirrored under `**name (via slack)**`, and a
guest's under `**name (guest, via slack)**`: a guest's name is their own
claim, not a member's.

A link works both ways on every service. Slack and Mattermost deliver
everything said in each channel the bot has been added to, and the bridge
hears only two kinds of channel: the control channel, and a channel with an
active link (a thread reply counts as the channel it lives in). Anywhere
else stays silent. A link made or retired takes effect with the next
message; the bot still has to be a member of the linked channel to hear it.

A message that arrived over a bridge carries `origin`:

```json
{ "backend": "discord", "surface_id": "C123", "external_message_id": "998" }
```

`thread_id` is set as well when it came in through a link to one thread.
Lantern shows it as a "via" badge; it is `null` for everything typed here.

Who somebody is on a bridge is theirs to prove, once:

| Route                                      | Needs | Result                                                                     |
| ------------------------------------------ | ----- | -------------------------------------------------------------------------- |
| `POST /v1/users/me/identities/link-code`   | write | `{code, expires_at}` — shown here and nowhere else, single use, 10 minutes |
| `GET /v1/users/me/identities`              | read  | `{data: [{backend, external_user_id, display_name, verified_at}]}`         |
| `DELETE /v1/users/me/identities/{backend}` | write | `204`; `404 identity_not_found`                                            |

The person types `!sbx link <code>` on the bridge, from the account they
want mapped. A message from an author nobody has mapped is refused with a
short reply pointing at that command — unless the link was created with
`allow_guests`, in which case it is stored as a person with no account,
under the name they use on that service. A map is only as good as the
membership behind it: an account removed from the workspace or deactivated
is unmapped again, and the link's `allow_guests` rule decides afresh.

The link is the authorization. A channel linked to a surface accepts what
anyone the bridge admits types there, a mapped account or a guest where the
link allows one, and the turn runs for that person without the `post` check
a turn started here makes: the channel's owner linked the surface, so
whoever may post on it may post in the channel, whether or not they could
open it in Lantern. A restart keeps the same rule: an accepted turn whose
message arrived over a bridge is recovered for its author, mapped or guest,
rather than dropped because that author cannot read the channel. Mentions
work as they do here: `@slug` in a linked message targets that agent and
joins it to the channel, and it is that agent that answers (so `@slug stop`
reaches it too); a message naming nobody is answered by Lantern. When a
linked message cannot be accepted, the surface hears a refusal only if it
was worded for people (a channel that is gone, say); any other failure is
reported as "check the daemon logs" and detailed there alone.

### Push notifications

When `/v1/capabilities` lists `push.apns_relay` (`[push] enabled` with a
`relay_url`), a signed-in person registers their devices for push
notifications. Every route acts for the caller only; someone else's device or
notification is `404`, like one that does not exist. A plain API client with
no local profile is `403 local_profile_required`.

| Route                                        | Needs | Result                                                                                                                                                      |
| -------------------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /v1/users/me/devices`                  | write | Body `DeviceIn`; `201` a new `DeviceOut`, `200` the one already registered with that token, updated; `409 device_limit_reached`, `502`, `503 push_disabled` |
| `GET /v1/users/me/devices`                   | read  | `DevicePage` `{items: [DeviceOut]}`, oldest first                                                                                                           |
| `DELETE /v1/users/me/devices/{device_id}`    | write | `204`; `404 device_not_found`                                                                                                                               |
| `POST /v1/users/me/devices/{device_id}/test` | write | `202 {ref}`: a `test` push queued to that device whatever its preferences; `404 device_not_found`, `409 device_not_enrolled`, `503 push_disabled`           |
| `GET /v1/users/me/notifications/{ref}`       | read  | `PushNotificationOut`; `404 notification_not_found`                                                                                                         |

```json
{
  "platform": "ios",
  "token": "<the device push token, 64 to 200 hex characters>",
  "env": "sandbox",
  "server_ref": "home",
  "name": "My phone",
  "prefs": {
    "mentions": true,
    "gates": true,
    "work": true,
    "failures": true,
    "per_channel": { "chn_…": "mentions" }
  }
}
```

`env` is `sandbox` or `production`, the gateway that issued the token.
`server_ref` (1 to 64 of `A-Z a-z 0-9 _ . : -`) is the client's own name for
this server, echoed in every push so a client registered with several servers
knows which to ask. `prefs` is optional: a new device gets every kind, an
update without it keeps what was stored. `per_channel` maps a channel id to
`all` (the default for an absent channel), `mentions` (only mentions of you)
or `none`.

Registration is an upsert keyed by the caller and the token (matched
case-insensitively). A new device, a change of `env`, and a device whose
handle the relay stopped recognising are enrolled with the push relay before
the answer: a relay that refuses is `502 push_relay_refused`, one that cannot
be reached or answers with nothing usable `502 push_relay_unavailable`, and
nothing is stored. Changing the name, `server_ref` or `prefs` does not call
the relay. `DeviceOut` is
`{id, platform, env, server_ref, name, prefs, token_suffix, created_at, updated_at, last_push_at}`:
`token_suffix` is the token's last six characters. The token is kept only as
a digest and is never returned; the relay's handle is never returned at all,
and neither is logged or put in an event.

A push carries only references — `{srv, k, ref, thread}`: the device's
`server_ref`, the kind, a notification ref and the channel id (empty when
there is none) — and the relay shows generic text. A notification service
extension fetches the real text with the person's own token:

```json
{
  "ref": "ntf_…",
  "kind": "mention",
  "channel_id": "chn_…",
  "turn_id": "trn_…",
  "title": "Ada Lovelace mentioned you",
  "body": "@grace can you take a look at this?",
  "created_at": "2026-09-25T12:00:00Z",
  "entry_id": null,
  "actions": [],
  "level": "active"
}
```

The record also says what the person can do about it, so a device can offer
it on the notification itself without opening the app:

- `entry_id` — the id of the [attention](#what-is-waiting-on-a-person)
  entry it is about (`gate:gate_…`, `item:itm_…:blocked:run_…`), or `null`
  when it is about none.
- `actions` — that entry's actions **its recipient may take**, by the
  attention list's own names (`gate_approve`, `retry`, `dismiss`, …), in the
  list's order: only those whose capability the recipient's role held when
  the notification was recorded, never one the server would refuse them.
  Each is taken through `POST /v1/attention/{entry_id}/act`, which checks the
  entry and the capability again as it stands then. Empty without an entry.
- `level` — how urgent it is: `passive` (news to read when convenient: work
  or a reply that arrived, the daily digest, a test push), `active` (worth a look now: a
  mention, something that could not finish, a plan waiting on you, a
  reminder about work that failed or is held) or `time_sensitive` (a
  decision is waiting on you: an opened gate, a job's `action_required`, a
  reminder about a decision).

| Source                                                   | `entry_id`                                                   | `actions`                                                      | `level`                                                |
| -------------------------------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------- | ------------------------------------------------------ |
| `gate.opened`                                            | `gate:<gate_id>` of the run's gate                           | `["gate_approve"]` (it goes only to who holds `gates:approve`) | `time_sensitive`                                       |
| A job's `action_required` attention                      | The entry about the job's run, else its item, when one waits | The entry's actions the recipient's role may take              | `time_sensitive`                                       |
| A job's `failure` attention                              | The same                                                     | The same (`retry`, `dismiss`, … where offered)                 | `active`                                               |
| `attention.reminder`                                     | The event's `entry_id`                                       | The event's `actions` the recipient's role may take            | `time_sensitive` for a `decision` entry, else `active` |
| Plan questions, a plan proposal, an epic run paused      | `null` (decided on the plan's own page)                      | `[]`                                                           | `active`                                               |
| A mention                                                | `null`                                                       | `[]`                                                           | `active`                                               |
| Work or a reply that could not finish                    | `null`                                                       | `[]`                                                           | `active`                                               |
| Work delivered, a reply finished, a job's `work`, a test | `null`                                                       | `[]`                                                           | `passive`                                              |
| The daily digest (`briefing.digest`)                     | `null`                                                       | `[]`                                                           | `passive`                                              |

A notification recorded before these fields existed reads `entry_id: null`,
`actions: []`, `level: "active"`. None of it rides the push itself: the
relay's payload is still exactly `{srv, k, ref, thread}` with the same five
kinds, and a device reads the rest from this record.

What is pushed, and to whom (never to the person whose message or action it
was):

| `kind`    | When                                                                                                                                                             | Title                                                                            |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `mention` | Another person's message names you as `@username` (word-bounded, any case) in a channel you can open; the body is the message on one line, cut to 140 characters | `<author> mentioned you`                                                         |
| `work`    | `collaboration.work.delivered` (not failed) or `collaboration.turn.completed` for a turn you asked for; a job's `work` attention to the channel's members        | `<agent> delivered <title>` / `<agent> replied` / the job's title                |
| `failure` | The same when the work ended `failed`, `blocked`, `cancelled` or `abandoned`, or `collaboration.turn.failed`; a job's `failure` attention                        | `<agent> could not finish <title>` / `<agent> could not reply` / the job's title |
| `gate`    | A job's `action_required` attention, or `gate.opened`, to workspace owners and admins who can see where it happened; one push per gate                           | the job's title / `Decision needed`                                              |
| `test`    | `POST …/test`                                                                                                                                                    | `Test notification`                                                              |

Planning adds three notices (#2349), each to **one person only** — never to
the channel, the rest of the workspace, or anyone else who can read the
plan — and each on an existing kind, so the device's `gates`, `work` and
`failures` switches govern them (and the push relay, which accepts only
these kinds, carries them unchanged):

| Notice                         | Event                       | `kind`    | Who                                                                                                                                                         | Title                                                  |
| ------------------------------ | --------------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Questions waiting for you      | `plan.generation.questions` | `gate`    | The person who asked for the breakdown: the author of the chat message behind the `plan` run's item, else the person whose `item.admit` operation queued it | `Questions waiting for you`                            |
| A proposal ready for you       | `plan.generation.proposed`  | `work`    | The same person                                                                                                                                             | `A proposal is ready for you`                          |
| An epic run you started paused | `plan.run.paused`           | `failure` | The person who started the epic run (`started_by`); for `reason: "person"`, only when someone else paused it                                                | `Your epic run needs you` / `Your epic run was paused` |

A breakdown or epic run whose asker is not an active member of the
workspace (a host-trusted operator, say) pushes nothing. The breakdown
notices carry the channel the breakdown was asked in when the asker can
open it (so per-channel preferences apply); the epic-run notice has none.

A thing that keeps waiting on a person is reminded about, on the existing
kinds again:

| Notice        | Event                | `kind`                                                                  | Who                                                                                                                                                                                                                                                                                                                                                                                            | Title                    |
| ------------- | -------------------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| Still waiting | `attention.reminder` | `gate` for a `decision` entry; `failure` for a `failed` or `paused` one | Among the people who can see where it is (a channel's readers for work it asked for; owners and admins for work nobody did; every member for a block on the daemon itself), those who hold the capability of at least one of the entry's actions (`capabilities` in the event) — or the owners, for an entry nobody below owner can act on. Never the whole workspace regardless of capability | `Still waiting: <title>` |

The body says how long it has waited and which reminder this is (`A decision has been waiting 4 hours. First reminder.`; `It ended blocked 2 days ago and still needs someone. Reminder 2.`). Each reminder is its own notice — the
second is not swallowed by the one-hour dedupe of the first — and the same
one is never pushed twice. The notice carries the entry's channel when it has
one, so per-channel preferences apply, and a device's `gates` or `failures`
switch governs it as it governs the kind.

An [escalation](#decisions-on-the-list) is a `decision` entry, so its
reminder is a `gate` at `time_sensitive`. Its `approve` and `decline` need
the escalated step's capability, which the event names per action
(`action_capabilities`, `{action: capability}`, beside `actions`): the
reminder reaches whoever holds it — a member for a plan approval or a
breakdown, owners and admins for a publish, a run or a retry, the owners
alone for a proposal of new work (`policy:manage`) — and each is offered
the actions their role may take.

The daily digest, when `[attention] digest_at` is set, is one more:

| Notice         | Event             | `kind` | Who                                                                          | Title                   |
| -------------- | ----------------- | ------ | ---------------------------------------------------------------------------- | ----------------------- |
| Daily briefing | `briefing.digest` | `work` | Every active member of the workspace with a device; never a plain API client | `Your Lantern briefing` |

The body is the digest's summary line (`Since yesterday 07:00: 11 landed, 1 failed; 2 waiting on a person; 9 decided under grants; runway 2.5 days.`),
cut to the notification body's length. It carries no channel, so the
device's `work` switch alone governs it, and its dedupe key is the day: one
digest is one push.

Only live events are pushed: the dispatcher reads the chronology from where
it stood when the daemon started, so historical events and a restart's replay
never are. Delivery retries a relay's retryable `502`, a `429` (at least its
`Retry-After`) and an unreachable relay with exponential backoff up to
`[push] max_attempts`; a `410` removes the device, and any `400` drops the
push. Notifications are kept as long as the chronology
(`[api] replay_retention_s`).

## Clients and tokens

Lantern issues its own tokens. A client is registered on the host with the
capabilities it may hold; its secret is printed once:

```bash
lantern api client create ci-reporter --cap runs:read --cap audit:read
lantern api client create deployer --cap runs:read --cap daemon:manage
lantern api client list
lantern api client revoke cli_…
lantern api key rotate           # the signing key; the previous one stands until its tokens expire
```

A client exchanges its secret for a short-lived access token and a rotating
refresh token:

```bash
curl -s -X POST http://127.0.0.1:8420/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"grant_type":"client_credentials","client_id":"cli_…","client_secret":"sk_…"}'
# {"token_type":"Bearer","access_token":"…","expires_in":900,"refresh_token":"rt_…","refresh_expires_in":604800}
curl -s -X POST http://127.0.0.1:8420/v1/auth/token \
  -d '{"grant_type":"refresh_token","refresh_token":"rt_…"}'
curl -s -X POST http://127.0.0.1:8420/v1/auth/revoke -H 'Authorization: Bearer …'
```

Rules a client can rely on:

- Bearer only: `Authorization: Bearer <access token>`, on every request and
  on the WebSocket's upgrade (or its first frame); never a cookie, never the
  query string.
- The access token is an Ed25519-signed JWT (`iss=lantern`, `aud=lantern-api`),
  valid for `[api] access_token_ttl_s` (15 minutes by default). Its
  capabilities are what the client held when it was minted **and still
  holds**: a grant narrowed later narrows the live token at once; a revoked
  client is refused on its next request and dropped from its streams.
- A refresh token is used once. Presenting it twice revokes its whole family
  (`401 refresh_reuse_detected`): its refresh tokens and every access token
  minted beside them, which are refused at once (`401 token_revoked`) and
  dropped from their streams. The client re-authenticates with its secret.
  Revoking a refresh token through `POST /v1/auth/revoke` does the same.
- Authentication failures are rate-limited per client id and per source
  address (`429`, with `Retry-After`).

### Capabilities

| Capability               | Grants                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| `runs:read`              | Every read: status, items, queue, runs, tasks, events, streams, gates, holds, schedules     |
| `items:create`           | `POST /v1/items`                                                                            |
| `runs:control`           | Cancel, resume, retry, requeue, abandon, re-arm the review wait, dismiss an alert           |
| `runs:steer`             | `POST /v1/runs/{id}/steering`                                                               |
| `budgets:grant`          | `POST /v1/runs/{id}/round-grants`                                                           |
| `gates:approve`          | `POST /v1/gates/{id}/approve`                                                               |
| `daemon:manage`          | Holds, stop, restart, repository resume, schedules                                          |
| `artifacts:read`         | Artifact catalogs and downloads                                                             |
| `audit:read`             | `GET /v1/operations`, `GET /v1/grants`, `GET /v1/decisions`                                 |
| `diagnostics:read`       | `GET /v1/logs`, `GET /v1/configuration`                                                     |
| `collaboration:read`     | Local profile, agent/team catalogs, channels, messages, preferences, workflows, connections |
| `collaboration:write`    | Local profile, teams, channels, preferences, and workflow mutations                         |
| `collaboration:delegate` | Accept a conversational or delegated channel turn                                           |
| `plans:create`           | Draft plans and edit their unpublished nodes                                                |
| `plans:publish`          | Publish a plan level to the forge, edit published nodes, run an epic                        |
| `policy:manage`          | Write, edit and delete grants (`/v1/grants`); only an owner holds it                        |

A refusal names the capability it needed (`403 forbidden` with
`"capability"`), before the target is looked at.

A workspace member's client holds exactly what their role grants. When a
release adds a capability to a role, the API grants it to every active
member's client as it starts; the member's next token refresh (or sign-in)
carries it.

`policy:manage` is the owner's alone. An owner holds every capability; an
admin holds every one except `credentials:manage` and `policy:manage`; a
member holds neither. An agent acting for itself never holds it, whoever it
is working for. A plain API client holds it only when the host operator
registered it with `--cap policy:manage`: counting as an owner where a route
asks for a role (a client holding `daemon:manage` does) is not holding the
capability. The routes that write a grant (see Delegation below) ask for the
capability, never for a role.

## Capability discovery

`GET /v1/capabilities` (any token) says what this installation serves:

```json
{
  "contract_version": 1,
  "server_version": "…",
  "workspace_id": "local",
  "features": ["status", "operations", "auth.client_credentials", "…", "schedules"],
  "capabilities": ["runs:read", "…"],
  "run_kinds": ["code", "workload", "tool", "plan"],
  "limits": {"page_default": 50, "page_max": 200, "max_body_bytes": 262144, "…": "…"},
  "retention": {"replay_s": 604800, "idempotency_s": 86400, "operation_deadline_s": 300}
}
```

`GET /v1/me` is the client as its token stands now. A client should read
`features` before relying on a route: a later release adds features; it does
not remove them within a contract version.

## Resources and ids

Every id is opaque and stable; none is an issue number, a host path or an
`owner/name`. `itm_…` a work item, `run_…` a run, `repo_…` a configured
repository, `gate_…` a merge or publication gate, `op_…` an operation,
`str_…` a steering record, `art_…` an artifact, `plan_…` a plan,
`node_…` one of its nodes and `erun_…` an epic run, `grant_…` a grant and
`dec_…` a decision, `evt_<n>` an event (and the
cursor into the chronology), `cli_…` a client. An unknown id of any kind is
a plain `404 not_found`. Each resource carries `workspace_id` (`"local"` on
a single installation), RFC 3339 UTC timestamps, `available_actions` (what
the daemon would accept for it right now — advice for a UI; the command is
rechecked when it arrives) and a `revision` a command may pin.

## Endpoint catalog

| Method   | Path                                                             | Capability             | Purpose                                                                     |
| -------- | ---------------------------------------------------------------- | ---------------------- | --------------------------------------------------------------------------- |
| `GET`    | `/health/live`, `/health/ready`                                  | none                   | Liveness; readiness with generation and projection lag                      |
| `GET`    | `/v1/capabilities`, `/v1/me`                                     | any                    | Contract, features, limits; the client's own grant                          |
| `GET`    | `/v1/openapi.json`                                               | none                   | The contract of record                                                      |
| `POST`   | `/v1/auth/token`, `/v1/auth/revoke`                              | none / any             | Mint and refresh; revoke the presented token                                |
| `POST`   | `/v1/auth/local/register`, `/login`                              | none                   | One local user's onboarding and login                                       |
| `GET`    | `/v1/auth/providers`                                             | none                   | The sign-ins a signed-out client may offer                                  |
| `POST`   | `/v1/auth/oidc/token`                                            | none                   | Redeem an OpenID Connect authorization code for a token pair                |
| `GET`    | `/v1/users/me`, `/v1/agents[/{slug}]`                            | collaboration read     | Local profile and product agent catalog                                     |
| `GET`    | `/v1/users`                                                      | workspace member       | The workspace directory                                                     |
| `PATCH`  | `/v1/workspace/members/{user_id}`                                | workspace admin        | Change a role; deactivate or reactivate a user                              |
| `DELETE` | `/v1/workspace/members/{user_id}`                                | workspace admin        | End a membership                                                            |
| `POST`   | `/v1/workspace/invites`                                          | workspace admin        | Create an invite; the raw token appears only here                           |
| `GET`    | `/v1/workspace/invites`                                          | workspace admin        | List invites                                                                |
| `DELETE` | `/v1/workspace/invites/{id}`                                     | workspace admin        | Withdraw an invite                                                          |
| `POST`   | `/v1/agents`, `/v1/agents/{slug}/archive`                        | collaboration write    | Save a person's own agent; archive it                                       |
| `PATCH`  | `/v1/agents/{slug}`                                              | collaboration write    | Edit a saved agent at the revision last read                                |
| CRUD     | `/v1/teams`, `/v1/channels`, `/v1/workflows`                     | collaboration          | Local teams, durable conversations, and workflow definitions                |
| CRUD     | `/v1/agents/{slug}/memories[/{id}]`                              | collaboration          | An agent's long-term memory, scoped by source channel                       |
| `GET`    | `/v1/channels/{id}/messages`                                     | collaboration read     | Immutable ordered conversation history                                      |
| `POST`   | `/v1/channels/{id}/turns`                                        | collaboration delegate | Accept an idempotent conversation/delegation turn                           |
| CRUD     | `/v1/channels/{id}/members`, `/participants`                     | collaboration          | The people and agents in a channel                                          |
| `GET`    | `/v1/bridges`                                                    | collaboration read     | The chat services a channel can be linked to                                |
| CRUD     | `/v1/channels/{id}/links`                                        | collaboration          | The bridge surfaces mirroring a channel                                     |
| CRUD     | `/v1/users/me/identities[/{backend}]`                            | collaboration          | Who you are on a bridge, and the code that proves it                        |
| CRUD     | `/v1/users/me/devices[/{id}[/test]]`                             | collaboration          | Devices registered for push notifications; a test push                      |
| `GET`    | `/v1/users/me/notifications/{ref}`                               | collaboration read     | What a push was about                                                       |
| CRUD     | `/v1/prompts`, `/v1/connections`                                 | collaboration          | User preferences; redacted connections and owner management                 |
| `GET`    | `/v1/status`                                                     | `runs:read`            | Live state: current run, queue, holds, breaker, stopping, watermark         |
| `GET`    | `/v1/items[/{id}]`, `/v1/queue`                                  | `runs:read`            | Work items; the queue in dispatch order                                     |
| `POST`   | `/v1/items`                                                      | `items:create`         | Admit an issue, a workload ask or a tool recipe                             |
| `POST`   | \`/v1/items/{id}/retry                                           | requeue                | abandon\`                                                                   |
| `POST`   | `/v1/items/{id}/dismiss`, `…/undismiss`                          | `runs:control`         | Acknowledge the item's alert for everyone; take that back                   |
| `GET`    | `/v1/runs[/{id}]`, `…/tasks`                                     | `runs:read`            | Runs and their tasks                                                        |
| `POST`   | `/v1/runs/{id}/dismiss`, `…/undismiss`                           | `runs:control`         | The same for a run no work item carries                                     |
| `GET`    | `/v1/attention`                                                  | `runs:read`            | Everything waiting on a person, with counts and the actions offered         |
| `POST`   | `/v1/attention/dismiss`                                          | `runs:control`         | Dismiss several named alerts under one operation                            |
| `POST`   | `/v1/attention/{id}/act`                                         | `runs:read`            | Take an action an entry offers (it needs that action's capability too)      |
| `POST`   | `/v1/items/{id}/delete`, `/v1/runs/{id}/delete`                  | `runs:control`         | Put finished work away: hidden from listings, run directories removed       |
| `POST`   | \`/v1/runs/{id}/cancel                                           | resume\`               | `runs:control`                                                              |
| `POST`   | `/v1/runs/{id}/steering`                                         | `runs:steer`           | Direction for the run in flight                                             |
| `GET`    | `/v1/runs/{id}/steering`                                         | `runs:read`            | Every instruction and its fate                                              |
| `POST`   | `/v1/runs/{id}/round-grants`                                     | `budgets:grant`        | More review rounds for an exhausted run                                     |
| `POST`   | `/v1/runs/{id}/review-wait/resume`                               | `runs:control`         | Re-arm a run parked for review                                              |
| `GET`    | `/v1/gates[/{id}]`                                               | `runs:read`            | Merge and publication gates                                                 |
| `POST`   | `/v1/gates/{id}/approve`                                         | `gates:approve`        | Endorse and release a gate at a revision                                    |
| `GET`    | `/v1/events`, `/v1/runs/{id}/events`                             | `runs:read`            | The chronology after a cursor                                               |
| `GET`    | `/v1/events/stream`                                              | `runs:read`            | The same, as server-sent events                                             |
| `WS`     | `/v1/ws`                                                         | `runs:read`            | Events and commands on one socket                                           |
| `GET`    | `/v1/runs/{id}/artifacts`                                        | `artifacts:read`       | The run's artifact catalog and where it published                           |
| `GET`    | `/v1/artifacts/{id}[/content]`                                   | `artifacts:read`       | One entry; its bytes as an attachment                                       |
| `GET`    | `/v1/runs/{id}/usage`, `/v1/usage`                               | `runs:read`            | Reported tokens and turns; never a bill                                     |
| `GET`    | `/v1/usage/pool`                                                 | `runs:read`            | Today's runs and tokens against the daily cap and budget                    |
| `GET`    | `/v1/analytics`                                                  | `runs:read`            | A window of runs folded: outcomes, time to land and parked, turns, causes   |
| `GET`    | `/v1/briefing`                                                   | `runs:read`            | What finished since, what waits on a person, what is lined up, the budget   |
| `GET`    | `/v1/operations[/{id}]`                                          | `audit:read`           | Every command any surface recorded                                          |
| `GET`    | `/v1/grants[/{id}]`                                              | `audit:read`           | The standing rules that let agents take decisions, with today's use         |
| `POST`   | `/v1/grants`                                                     | `policy:manage`        | Let an agent take a delegable action, under conditions                      |
| `PATCH`  | `/v1/grants/{id}`                                                | `policy:manage`        | Edit a grant's conditions, limit, note or switch at the revision read       |
| `DELETE` | `/v1/grants/{id}`                                                | `policy:manage`        | Delete a grant; the decisions it allowed stay in the ledger                 |
| `GET`    | `/v1/decisions`                                                  | `audit:read`           | What was decided for agents: allowed, denied or escalated to a person       |
| `GET`    | `/v1/repositories`, `/profiles`, `/recipes`                      | `runs:read`            | What work may be admitted against                                           |
| `POST`   | `/v1/repositories/{id}/resume`                                   | `daemon:manage`        | Poll a suspended repository again                                           |
| `POST`   | `/v1/repositories/{id}/labels/sync`                              | `daemon:manage`        | Create the labels the loop applies that the repository is missing           |
| `GET`    | `/v1/repositories/available`                                     | owner role             | What the host's forge credential can see, to pick one to register           |
| `POST`   | `/v1/repositories`                                               | `daemon:manage`        | Register a repository; polled from the next start                           |
| `PATCH`  | `/v1/repositories/{id}`                                          | `daemon:manage`        | Enable, disable or re-base a registered repository                          |
| `DELETE` | `/v1/repositories/{id}`                                          | `daemon:manage`        | Forget a registration; queued and running work is untouched                 |
| `GET`    | `/v1/daemon/holds`                                               | `runs:read`            | Standing holds and whose they are                                           |
| `POST`   | `/v1/daemon/holds`                                               | `daemon:manage`        | Take a hold attributed to this client                                       |
| `DELETE` | `/v1/daemon/holds/{name}`                                        | `daemon:manage`        | Release your hold; `?force=true` overrides another's                        |
| `POST`   | `/v1/daemon/stop`, `/v1/daemon/restart`                          | `daemon:manage`        | Graceful stop; a stop the supervisor undoes                                 |
| `GET`    | `/v1/schedules[/{name}]`                                         | `runs:read`            | Schedules with cadence, last and next due                                   |
| `PATCH`  | `/v1/schedules/{name}`                                           | `daemon:manage`        | Atomically replace or rename a schedule while preserving run history        |
| `POST`   | \`/v1/schedules\[/{name}/pause                                   | resume\]\`             | `daemon:manage`                                                             |
| `DELETE` | `/v1/schedules/{name}`                                           | `daemon:manage`        | Remove                                                                      |
| `GET`    | `/v1/logs`, `/v1/configuration`                                  | `diagnostics:read`     | The log ring, redacted; the allowlisted configuration with provenance       |
| `GET`    | `/v1/plans[/{id}]`                                               | `runs:read`            | Plans and their nodes, drafts included                                      |
| CRUD     | `/v1/plans[/{id}[/nodes[/{node_id}]]]`                           | `plans:create`         | Draft a plan, edit it, add, edit, move and remove nodes                     |
| `POST`   | `/v1/plans/{id}/nodes/{node_id}/approve`                         | `plans:create`         | Approve a node's draft and proposed children; recorded as an operation      |
| `POST`   | `/v1/plans/{id}/nodes/{node_id}/publish`                         | `plans:publish`        | Publish one level to the forge; `Idempotency-Key` required                  |
| `PATCH`  | `/v1/plans/{id}/nodes/{node_id}` (published)                     | `plans:publish`        | Edit a published node's sections: writes its issue                          |
| `POST`   | `/v1/plans/{id}/nodes/{node_id}/attach`                          | `plans:publish`        | Attach an existing open issue as a child                                    |
| `POST`   | `/v1/plans/{id}/nodes/{node_id}/detach`                          | `plans:publish`        | Unlink a child from its parent; its issue stays open                        |
| `POST`   | `/v1/plans/{id}/sync`                                            | `plans:create`         | Reconcile the plan from the forge now                                       |
| `POST`   | `/v1/plans/{id}/drift/ack`                                       | `plans:create`         | Mark the forge's changes to a plan seen                                     |
| `POST`   | `/v1/plans/{id}/nodes/{node_id}/breakdown`                       | `plans:create`         | Queue a `plan` run proposing the node's next level                          |
| `POST`   | `/v1/plans/{id}/nodes/{node_id}/answers`                         | `plans:create`         | Answer or skip a breakdown's clarifying questions; resumes its run          |
| `POST`   | `/v1/plans/{id}/nodes/{node_id}/replan/approve`                  | `plans:publish`        | Apply a re-plan's diff through the publish path; `Idempotency-Key` required |
| `POST`   | `/v1/plans/{id}/nodes/{node_id}/replan/discard`                  | `plans:create`         | Discard a re-plan's diff                                                    |
| `POST`   | `/v1/plans/{id}/nodes/{epic_id}/run`                             | `plans:publish`        | Run an epic: admit its ready tasks in dependency order                      |
| `GET`    | `/v1/plans/{id}/nodes/{epic_id}/run`                             | `runs:read`            | An epic's most recent run, each task's state, item and run                  |
| `POST`   | `/v1/plans/{id}/nodes/{epic_id}/run/pause`, `/resume`, `/cancel` | `plans:publish`        | Hold, resume or stop an epic run's admission; `Idempotency-Key` required    |
| `POST`   | `/v1/plans/{id}/nodes/{task_id}/run/retry`, `/skip`              | `plans:publish`        | Retry a failed task of an epic run, or treat a task as done                 |

Every collection pages by an opaque `cursor` bound to its filters
(`limit` up to 200; `{"data": […], "next_cursor": …, "has_more": …}`). The
[user guide](user-guide.md#the-remote-api) walks each group with examples.

## Commands, operations and idempotency

Every mutation is one **operation**: accepted durably (its row, its
idempotency record and its audit event in one transaction) before it acts,
claimed under the daemon's generation, applied through the same service
`ctl` and chat use, finished with its outcome. The reply carries the
operation; `GET /v1/operations/{id}` is the record afterwards, and the
operation's transitions ride the chronology.

- **Idempotency.** Send `Idempotency-Key` on any mutation; `POST /v1/items`
  requires it. The key is scoped to the workspace, the client, the method and
  the route, so two clients' keys never collide. A replay with the same body
  answers with the operation that already exists (`200`, `created: false`);
  a different body under the same key is `409 idempotency_conflict`; a
  replay while the first attempt is still being applied is
  `409 already_in_progress`. Keys are kept for `[api] idempotency_retention_s`.
- **Revisions.** Pass `expected_revision` to act on exactly the state you
  read; a state that moved is `409 stale_revision`. Gate approval requires
  it.
- **Acceptance is not completion.** A cancel answers `202` and is honoured
  at the run's next boundary; a gate approval answers `202` and the landing
  completes afterwards (`gate.resolved` in the chronology); a stop or restart
  answers `202` the moment it is durably accepted, before the process exits.
  Watch the operation or the chronology for the effect.
- **Bots get one answer.** A command refused by policy is a recorded
  operation in state `failed` with its `error_code`; retrying it is a new
  operation, not a fix loop.

### Dismissing an alert

Work that finished without success, or is parked on a person, asks for
attention until someone acts on it — and sometimes the right act is none: the
failure is understood, nobody wants a retry. `POST /v1/items/{id}/dismiss`
(feature `work.dismiss`; `runs:control`; body
`{"reason": …, "expected_revision": …}`, both optional) records that
acknowledgement **for everyone**: the item, its run, its gate and its rows in
`/v1/channels/{id}/work` and `/jobs` carry
`dismissal: {at, by, cause, reason, operation_id}`, and a client leaves a
dismissed row out of whatever it shows as needing attention.

- **Nothing about the work changes.** Its state, its `revision` and its other
  `available_actions` stay as they were — a dismissed failure can still be
  retried, a dismissed gate still approved. A command sent with the revision
  read before the dismissal is not stale.
- **Where it applies.** `dismiss` is in an item's `available_actions` when the
  item is `failed`, `blocked`, `cancelled`, `gated`, `awaiting_review`,
  `paused_review` or `awaiting_answers`, or is `queued` behind a run parked
  `provider_held`. Anything else is `409 not_eligible` with the state named.
  Dismissing twice answers `200` with the first dismissal.
- **It ends by itself.** The moment the item changes state the dismissal is
  gone: a retry that fails again is a new alert. `POST …/undismiss` takes it
  back by hand; `undismiss` replaces `dismiss` in `available_actions` while a
  dismissal stands.
- **Giving work up dismisses it.** A person's abandon — the item's
  `abandon` route, the chat and `ctl` verbs, the CLI, stopping an epic run
  that withdraws its queued items — leaves the item `failed` *and*
  dismissed, with `cause: "abandoned"`: the person has seen what they gave up.
  The daemon's own abandon (a pull request closed unmerged) dismisses nothing;
  nobody has looked at that yet. A cancelled run needs no dismissal: it rests
  in `cancelled`, which is not a failure.
- **Too late to give up.** A run at its `publishing` stage is handing its
  result to the sinks, and nothing can take that back: abandoning its item
  then is `409 not_eligible` ("run is publishing its result"), and `abandon`
  leaves `available_actions`. An abandon that arrived just before, whose
  cancel the run never honoured, does not outrank what the run did: a run
  that ended delivered (`completed` with nothing to land, or `merged`)
  settles its item `done`, not `failed`.
- **A run without an item.** Work an item carries is dismissed through the
  item; `POST /v1/runs/{id}/dismiss` on such a run leaves the same mark. A run
  nothing pins — its item row is gone, or has moved on to a later attempt —
  advertises `dismiss` itself when it is `failed`, `blocked`, `cancelled`,
  `gated`, `awaiting_review`, `held`, `provider_held` or `awaiting_answers`,
  and its dismissal ends when the run changes state.

The operation (`item.dismiss`, `item.undismiss`, `run.dismiss`,
`run.undismiss`) is the record of who and when; the same four are command
actions on `/v1/ws`.

**Several at once.** `POST /v1/attention/dismiss` (feature
`work.dismiss_all`; `runs:control`) dismisses up to 200 alerts under one
`attention.dismiss_all` operation:

```json
{
  "reason": "cleared the board",
  "targets": [
    {"item_id": "itm_…", "expected_revision": 7},
    {"run_id": "run_…"}
  ]
}
```

The request names each alert — the ones the person was looking at; there is no
"everything", because what they were shown may have changed since it was
drawn. With `attention`, those are the entries of
[`GET /v1/attention`](#what-is-waiting-on-a-person) that offer `dismiss`: send
each one's `item_id` — with `expected_revision` only for an `item` entry,
whose `revision` is the item's — and the entry leaves the list for everyone.
The answer is `{operation, results}` with one
result per target in the request's order: `dismissed`, `already_dismissed`, or
`skipped` with the `code` and `detail` the single route would have refused
with (`not_found`, `not_eligible`, `stale_revision`). A skipped target does not
fail the others.

### Deleting finished work

Dismissing takes an alert off the list of things to look at; the work stays in
every other listing. `POST /v1/items/{id}/delete` (feature `work.delete`;
`runs:control`; body
`{"reason": …, "expected_revision": …, "discard_undelivered": false}`, all
optional) puts the work away:

- **Hidden, not erased.** The item and every run it had leave `GET /v1/items`,
  `GET /v1/runs` and a channel's `/work` and `/jobs`. The rows, the event trail
  and the operation stay — they are the audit record — and a read of the
  item or the run by its id still answers, with `deleted_at` set and no
  `available_actions`. `?include_deleted=true` on either listing shows
  them again.
- **The disk is reclaimed.** Each run's sandboxes and run directory are removed
  now instead of at the retention sweep, recorded with the same `daemon.gc`
  event; a deleted run cannot be resumed.
- **The forge is not touched.** The pull request, the branch and the issue stay
  as they are: they belong to the target repository.
- **Only work at rest.** `delete` is in `available_actions` for an item that is
  `done`, `failed`, `blocked` or `cancelled` and has no run in flight. Anything
  queued, running or parked on a decision is `409 not_eligible` — abandon or
  cancel it first; a delete never doubles as a way to stop something.
- **Undelivered work is kept.** When a run's delivery failed, or its sandboxes
  were kept, its workspace is the only copy of what the run produced: the
  delete is refused — `409 not_eligible` with `"undelivered": true` in the
  problem body — unless `discard_undelivered` is `true`.
- **No further command.** Deleted work refuses every control with
  `409 not_eligible` "work was deleted". The source asking for the work again
  is not a command: an issue re-admitted comes back as a fresh item, visible
  again, while its deleted runs stay hidden.

`POST /v1/runs/{id}/delete` deletes the item when one pins the run, and the run
alone when nothing does. Deleting twice answers `200`. `item.delete` and
`run.delete` are command actions on `/v1/ws`.

## Following the work: events, SSE and the WebSocket

The **chronology** is one durable, ordered stream: the daemon's notices, a
run's start and finish, its engine events (every persisted one, `worker.stdout`
included — filter with `type_prefix`), gate transitions, steering receipts,
every operation any surface recorded, and — with `attention.act` —
[`attention.opened`, `attention.resolved` and `attention.reminder`](#hearing-that-an-entry-appeared-or-left)
when something starts and stops waiting on a person, and while it still
does, and — with `[attention] digest_at` set — the day's
[`briefing.digest`](#the-daily-digest). Each event's `id` (`evt_<n>`) is
also the cursor.

1. Read a snapshot: `GET /v1/status` reports `watermark`.
2. Read what came after it: `GET /v1/events?after=evt_<watermark>` — pages
   neither gap nor repeat.
3. Subscribe from the last id you saw: `GET /v1/events/stream` with
   `Last-Event-ID` (or `?after=`), or `subscribe{after}` on `/v1/ws`.
4. On a disconnect, reconnect from the last id. The stream says why it
   closed (`stream.closed` / `closing{reason}`: `daemon_stopping`,
   `client_revoked`, `token_expired`, `access_revoked`).

History is kept for `[api] replay_retention_s`; a cursor older than what
remains is `410 cursor_expired` with a pointer to the snapshot, never a
silent skip. A read of one run (`/v1/runs/{id}/events`, or `run_id` on the
stream or the socket) is refused only when retention took some of that run's
own events: a run begun after the last prune replays whole from its start.
For one whose start is gone, `GET /v1/events?run_id=…&latest=true` still
serves the newest events held. The WebSocket takes the same typed commands as REST
(`command{id, action, target, params, idempotency_key, expected_revision}`,
answered by `reply{id, ok, result | problem}`) — `item.*`, `run.*`,
`gate.approve`, `daemon.*`, `repository.resume`, `schedule.*` — with the same
idempotency scope; a stop or restart sent on the socket takes effect after
its reply frame.

### Steering one task, or one agent

`POST /v1/runs/{id}/steering` takes two optional targets beside `text`.

- `task_id` addresses one task lane. The instruction waits in that task's
  own mailbox and is answered when *that* task reaches a phase boundary,
  which is what makes steering meaningful with `[budgets] max_parallel_tasks` above 1: without it the lane that answers is
  whichever one got to a boundary first. A task that finishes with
  instructions still waiting hands them to the run, where they are answered
  as run-level direction rather than dropped.
- `agent_slug` names an agent on the run's assignment. The answer comes
  back in that agent's persona and with its model, and the run's
  `chat.reply` event carries `agent_slug` so a reader can attribute it.

Both are optional and neither is required to exist: an instruction that
names no target, or a target this run does not have, is answered exactly as
it always was. A daemon that does not list `collaboration.mention_steering`
ignores both fields, so sending them is safe against an older server.

In a channel, `collaboration.mention_steering` means a mention of an agent
already working live work there is taken as direction for that run instead
of starting a fresh answer: the turn's `steered_run_id` names the run. The
mention has to be unambiguous -- one live run in the channel with that agent
on it -- or it stays an ordinary turn. Stopping stays explicit. A bare `/stop` or
`/cancel` does exactly what `POST /v1/channels/{id}/stop` does: it cancels
the channel's other turns, cancels the runs the channel asked for, abandons
the work items it queued that have not started, and silences the channel
for the same hour; the reply names each run, item and turn it stopped, or
says nothing was running or queued, and that the channel is quiet. Exactly
`@agent stop` is narrower: it cancels that agent's runs in the channel and
nothing else, through the same cancel the API's `POST /v1/runs/{id}/cancel`
uses. A message that merely argues for stopping is steering, not a stop.

Both act as the person who wrote the message. A steer takes the
capabilities their workspace role grants (`runs:steer`, which a `member`
holds). A stop takes the rule `POST /v1/channels/{id}/stop` takes: anyone
who may post in the channel may stop the work that channel asked for,
without `runs:control`, so a plain `member` may stop as well as steer. The
cancel is recorded in the person's name and reaches only that channel's
work; someone who may not post there is told nothing was stopped. Only a
message the person wrote steers or stops: a turn another agent started
never does, and an agent reached through another agent's handoff answers
the request it was handed.

### Who sees which events

When `/v1/capabilities` lists `events.scoped`, every event is recorded with
the channel it belongs to (its own channel, the channel a memory was learned
in, or the channel that asked for its run or item) and, for a person's own
teams, preferences, workflows and profile, the one user it is for. The page routes, the run's events, the SSE
stream and the WebSocket all filter in the query, so a page is never short
and `has_more` means what it always meant:

- A plain API client (no workspace member behind it) sees every event.
- Every workspace member sees events meant for everyone or for them alone.
- Every workspace member, owners and admins included, sees the events of
  the channels they can open (their channels and every workspace channel).
  A private channel's events reach its members only: a workspace owner or
  admin who is not a member of it does not see them.
- A run's or an item's events are shown only when a channel the member can
  open asked for the work (a workload or tool run started from a chat
  message, or a code run whose issue a chat turn filed).
- Work no channel asked for (a run or an item with no channel, and run
  events recorded before this release) is shown to workspace owners and
  admins only. A plain member sees, beyond their channels, only events with
  no channel, no run and no item.
- A person's own team, preference, workflow and profile events recorded
  before `events.scoped` are given that person as their audience on
  upgrade; one whose person can no longer be told reaches no member.

A live subscription moves its cursor past events its member may not see,
so it does not scan them again. Membership changes apply from the next
read (the stream and the socket re-read the member when they re-check the
token). A stream or socket a member opened is pinned to that member: once
they are removed from the workspace, or their client loses `runs:read`, it
closes with `access_revoked` at the next re-check (the socket with close
code 4403) and delivers nothing further, even while their access token
still verifies; it never widens to the unfiltered view a plain API client
gets. `GET /v1/events` and `GET /v1/events/stream` accept
`channel_id=<chn_...>` to follow one channel.

## What is waiting on a person

When `/v1/capabilities` lists `attention`, `GET /v1/attention` (`runs:read`)
answers "what needs someone" as one list, so a client no longer joins items,
gates, the queue, plans and every channel's work to find out — and two clients
no longer disagree about it. It is computed on read from what the daemon
already keeps; nothing is stored, and reading it changes nothing.

One entry per thing a person has to act on:

| `kind`          | What waits                                                                                 | `id`                                           | `group`    |
| --------------- | ------------------------------------------------------------------------------------------ | ---------------------------------------------- | ---------- |
| `gate`          | An open merge or publication gate. The item it parks is the same entry, never a second one | `gate:<gate id>`                               | `decision` |
| `item`          | An item `awaiting_review`, `paused_review` or `awaiting_answers`                           | `item:<item id>:<state>[:<run id>]`            | `decision` |
| `item`          | An item that ended `failed` or `blocked`                                                   | `item:<item id>:<state>[:<run id>]`            | `failed`   |
| `epic_task`     | A `failed` task of an epic run that is `running` or `paused`                               | `epic_task:<epic run id>:<node id>[:<run id>]` | `failed`   |
| `provider_hold` | A provider hold with no retry scheduled ("explicit operator recovery required")            | `provider_hold:<backend>:<generation>`         | `paused`   |
| `repository`    | A repository whose polling is suspended                                                    | `repository:<repository id>`                   | `paused`   |

With `attention.decisions` three more — see
[Decisions on the list](#decisions-on-the-list):

| `kind`           | What waits                                                                                  | `id`                                    | `group`    |
| ---------------- | ------------------------------------------------------------------------------------------- | --------------------------------------- | ---------- |
| `escalation`     | A step an agent asked to take that no grant covered, unresolved in the decisions ledger     | `escalation:<decision id>`              | `decision` |
| `plan_questions` | A `manual` plan's breakdown questions awaiting answers that no parked item stands for       | `plan_questions:<plan>:<node>:<run id>` | `decision` |
| `plan_proposal`  | A `manual` plan's level the planner proposed (one or more `proposed` children) not approved | `plan_proposal:<plan id>:<node id>`     | `decision` |

- **`kind` is open.** A later release adds kinds. A client still shows an entry whose
  `kind` it does not know — as a plain row with its `title` and `reason` and
  no controls — rather than hiding it or failing the page: something that
  waits on a person is worse hidden than plain. `counts` includes it.
- **`id` is opaque and stable.** The same thing waiting keeps its id from one
  read to the next. When it stops waiting the entry is gone, and when it waits
  again in a new way — a review wait that paused, a retry that failed on a new
  run — it is a new entry under a new id. Read the reference fields, not the
  id.
- **What is not an entry.** Work that is queued, running or done. A gate being
  approved (the landing is the daemon's to finish; a failed approval reopens
  the gate and the entry). A `cancelled` item: stopping work is a person's own
  act, the item rests there asking nothing, and although `dismiss` is accepted
  on it none is needed. A task `blocked` behind a failed one (it waits on the
  same decision). A failed task of a stopped epic run (it takes no retry and
  no skip; its failed item is still an `item` entry). A provider hold with a
  time to try again, a repository merely backing off, and a named pause hold
  — the first two end by themselves and the third is someone's own act.
- **Dismissed and deleted.** An entry whose item carries a `dismissal` is left
  out — that includes work a person abandoned, which rests `failed` and
  dismissed — unless `include_dismissed=true`, which lists it with
  `dismissal` set (its `cause` tells an acknowledged failure from work given
  up). Deleted work never appears. An `epic_task` entry is not dismissed with
  its item: the run still cannot finish until the task is retried or skipped.

Each entry carries:

- `id`, `kind`, `group` (`decision`, `failed` or `paused`) and `state` — the
  state word of what waits, in its own vocabulary (`gated`, an item state,
  `failed` for a task, `provider_held`, `suspended`).
- `title`, `reason` (the gate's detail, the item's last error or its run's
  reason, the task's reason, the hold's summary, why polling stopped; `null`
  when nothing was recorded) and `since`, when it started waiting (the gate's
  creation, the item's or the task's last change, the hold's last failure, the
  repository's first failed poll).
- `repository` (`owner/name`) and `repository_id`, `null` for work no
  repository asked for.
- References, each `null` when it does not apply: `item_id`, `run_id`,
  `gate_id`, `plan_id`, `node_id`, `epic_run_id` and `channel_id`. An `item`
  entry for work an epic run admitted names the run in `epic_run_id`; an
  `epic_task` entry names its item and that item's run when it has them.
- `revision`: the gate's for a `gate` entry, the item's for an `item` entry,
  `null` otherwise — what an [act on the entry](#acting-on-an-entry) sends as
  `expected_revision`.
- `actions`: `{action, capability, allowed}` for every action the server
  offers on the entry right now, the act that settles the wait first and the
  ones that give the work up or put the alert away last. They are what the
  item, its run and its gate advertise in `available_actions` (`gate_approve`,
  `review_wait_resume`, `grant_rounds`, `resume`, `retry`, `requeue`, `steer`,
  `cancel`, `abandon`, `dismiss`, `undismiss`, `delete`), plus `task_retry`
  and `task_skip` on an `epic_task` (the epic run's `…/run/retry` and
  `…/run/skip`) and `repository_resume` on a `repository`. `capability` is the
  one the action's route requires and `allowed` whether the caller holds it,
  so a client shows a member the decision without offering a button that
  would be refused. A provider hold lists none: it is recovered from the host
  or a chat (`resume <backend>`), not over the API.
- `dismissal`, set only on an entry listed with `include_dismissed`.

| Query               | Default | Meaning                                                      |
| ------------------- | ------- | ------------------------------------------------------------ |
| `group`             | all     | Repeatable: only these groups. Anything else is `422`        |
| `repository_id`     | all     | Only what belongs to this repository; an unknown id is `404` |
| `include_dismissed` | `false` | Also list entries whose alert was dismissed                  |
| `limit`, `cursor`   | 50      | As every collection; a cursor is bound to the filters it had |

The response is `{data, next_cursor, has_more, counts, observed_at}`. Entries
come `decision` first, then `failed`, then `paused`, the longest wait first
within each. `counts` is `{total, decision, failed, paused}` over everything
that matches `repository_id` and `include_dismissed` — whatever `group` and
`limit` the page had — so `GET /v1/attention?limit=1` is enough to badge, and
one page badges every tab.

**Who sees what.** The list is workspace-wide: exactly the items and gates
`GET /v1/items` and `GET /v1/gates` already show a `runs:read` holder, with no
per-channel filter. `channel_id` alone is withheld — it is set only when the
caller can read that conversation, as on `GET /v1/items`.

### Decisions on the list

When `/v1/capabilities` lists `attention.decisions`, what agents and plans
wait on a person for is on the same list.

**Escalations.** Every `escalate` row of the decisions ledger
([`GET /v1/decisions`](#delegation)) not yet resolved is an `escalation`
entry: `state` `escalated`, `title` naming in plain words what the agent
wanted to do (`planner asks to publish the level under “Reports”`), `reason`
the judge's reason, `since` when it was decided, the row's references
(`plan_id`, `node_id`, `item_id`, `run_id`, `epic_run_id`, `repository`) and
three fields of its own: `agent` (the slug), `decision_id` and
`decision_action` (the delegable action). `revision` is the plan's for a plan
step and the item's for an item or run step.

It offers `decline` and, where a person can take the step here, `approve`.
Both need the capability a person needs to take that step themselves:

| `decision_action`  | `approve` runs, as the caller                                       | `capability`    |
| ------------------ | ------------------------------------------------------------------- | --------------- |
| `plan.breakdown`   | `POST /v1/plans/{id}/nodes/{node}/breakdown` (`202`)                | `plans:create`  |
| `plan.approve`     | `POST …/nodes/{node}/approve`, every draft and proposed child       | `plans:create`  |
| `plan.publish`     | `POST …/nodes/{node}/publish`                                       | `plans:publish` |
| `plan.run`         | `POST …/nodes/{node}/run` (`201`)                                   | `plans:publish` |
| `plan.run.retry`   | `POST …/nodes/{task}/run/retry`                                     | `plans:publish` |
| `item.retry`       | `POST /v1/items/{id}/retry`                                         | `runs:control`  |
| `run.grant_rounds` | `POST /v1/runs/{id}/round-grants`, `params.rounds` (else the row's) | `budgets:grant` |
| `plan.propose`     | — no human path: `decline` only                                     | `policy:manage` |

An action this release does not know offers `decline` only, under
`policy:manage`. `approve` records the step's own operation under the person
(its refusals are that route's, and a refused step leaves the escalation
waiting), then resolves the decision `acted` with the person as
`resolved_by`. `decline` records a `decision.decline` operation that resolves
it `declined` by the person and changes nothing else. Either way the act's
answer carries `decision`, the ledger row as it then stands.

The list resolves an escalation in two ways only: a person's `approve` or
`decline` on it, and its target being gone — its plan deleted or archived,
its node removed, its item gone or deleted. Such an escalation leaves the
list the next time the list is read, and the attention tracker resolves it
`superseded` (no `resolved_by`) on its next pass; the read itself never
writes. Whether the step already happened, or the situation that asked for
it moved on, is judged by the pass that escalated it — the plan driver for
the plan steps, triage for the retries and round grants — which resolves its
own escalations `acted` or `superseded`; until it does, the entry stays.

**A manual plan's questions and proposals.** Only on a plan whose `advance`
is `manual`; a plan that advances itself shows neither — it reaches a person
only through its escalations.

- A breakdown that asked questions parks its `plan` item `awaiting_answers`,
  and that `item` entry *is* the questions' entry: it keeps the id clients
  already key on, and dismissing it puts the questions away. A
  `plan_questions` entry stands only for questions no such item stands for.
  It offers no action: questions are answered on the plan's page.
- A `plan_proposal` is one node whose children include `proposed` ones,
  `title` counting them, `reason` naming who proposed them. It offers
  `approve` (`plans:create`) — to a caller holding `plans:create` only; others
  see no action — which runs the plan's approve route on every draft and
  proposed child and answers `{plan, operation_id, replayed}`.

### Acting on an entry

When `/v1/capabilities` lists `attention.act`,
`POST /v1/attention/{id}/act` takes one of the actions an entry offers, naming
nothing but the entry and the action — what a notification's button holds. It
is routing and nothing else: the entry is looked up as it stands now (a
dismissed one included, so `undismiss` works), and the request is handed to
the command the action's own route runs. The operation recorded, its
refusals and its effect are that command's; nothing is recorded for the act
itself, so `GET /v1/operations` shows each act once.

```json
{"action": "gate_approve", "expected_revision": 3, "params": {}}
```

- **`action`** is one of the entry's `actions`. The caller needs `runs:read`
  for the route and the action's own `capability` for the act
  (`403 forbidden` naming the capability otherwise).
- **`params`** are the action's own arguments — the fields its own route
  takes in its body, validated by the same model, so an unknown or
  ill-typed one is `422 invalid_request` with `errors` (`loc` begins
  `params`).

| `action`                          | The route it runs                                         | `params`                                                  |
| --------------------------------- | --------------------------------------------------------- | --------------------------------------------------------- |
| `gate_approve`                    | `POST /v1/gates/{id}/approve`                             | none                                                      |
| `review_wait_resume`              | `POST /v1/runs/{id}/review-wait/resume`                   | none                                                      |
| `grant_rounds`                    | `POST /v1/runs/{id}/round-grants`                         | `rounds` (required)                                       |
| `resume`                          | `POST /v1/runs/{id}/resume`                               | none                                                      |
| `cancel`                          | `POST /v1/runs/{id}/cancel`                               | `retry`                                                   |
| `steer`                           | `POST /v1/runs/{id}/steering`                             | `text` (required), `source_refs`, `task_id`, `agent_slug` |
| `retry`, `requeue`                | `POST /v1/items/{id}/retry`, `…/requeue`                  | none                                                      |
| `abandon`, `dismiss`, `undismiss` | `POST /v1/items/{id}/abandon`, `…/dismiss`, `…/undismiss` | `reason`                                                  |
| `delete`                          | `POST /v1/items/{id}/delete`                              | `reason`, `discard_undelivered`                           |
| `task_retry`, `task_skip`         | `POST /v1/plans/{id}/nodes/{task_id}/run/retry`, `…/skip` | none                                                      |
| `repository_resume`               | `POST /v1/repositories/{id}/resume`                       | none                                                      |
| `approve`, `decline`              | See [Decisions on the list](#decisions-on-the-list)       | `rounds` on an escalated round grant; otherwise none      |

- **`expected_revision`** is the entry's `revision` as the person read it. It
  is never defaulted from the entry as it stands: the point of it is that the
  person acted on what they saw. How each kind supplies it:

  - A `gate` entry carries its gate's revision. `gate_approve` **requires**
    it (`422 invalid_request`, `errors[].loc` `["expected_revision"]`,
    without) and the approval binds to it exactly as on the gate's own route.
  - An `item` entry carries its item's revision. The item actions (`retry`,
    `requeue`, `abandon`, `dismiss`, `undismiss`, `delete`) hand it to their
    command, which checks it inside its own operation.
  - Every other pairing — a run's action (`cancel`, `resume`, `grant_rounds`,
    `steer`, `review_wait_resume`) on an `item` entry, any action but the
    approval on a `gate` entry — runs a command that checks some other
    record's revision or none. There the entry's revision is compared before
    the command runs and the command is sent none.
  - An `epic_task`, a `repository` and a `provider_hold` entry have
    `revision: null`: the epic run's retry and skip, and a repository's
    resume, take no revision. Sending one is `422 invalid_request`.
  - An `escalation` or a `plan_proposal` carries the plan's revision (an
    item's, for an item or run step). One sent is compared with the entry
    before anything runs. A plan step is then sent the revision the entry
    has as it is acted on (on a replay, the one the first act sent), so
    the step's own route still refuses a plan that moved in between.

  Wherever it is checked, a moved entry is `409 stale_revision` with the
  current `revision`. Optional everywhere but on `gate_approve`.

- **`Idempotency-Key`** is required (`422 idempotency_key_required`). The key
  is scoped to the workspace, the caller and this entry, and it is the key
  the action's operation is recorded under. A replay answers the first act
  (`replayed: true`, the same `operation_id`) — also once the entry is gone,
  which after a successful act it usually is; a refusal the command recorded
  replays as that refusal. Another action, or other `params`, under the same
  key is `409 idempotency_conflict`. A refusal made here, before any command
  ran (`not_waiting`, `not_eligible`, `forbidden`, a `422`), records nothing
  and consumes no key.

The answer is `200` — or `202` where the action's own route answers `202`: a
gate approval, a steer, a cancel honoured at the run's next boundary — with
`Location: /v1/operations/{id}`:

```json
{
  "entry_id": "gate:gate_x1",
  "action": "gate_approve",
  "operation_id": "op_…",
  "replayed": false,
  "still_waiting": false,
  "result": {"gate": {"…": "…"}, "operation": {"…": "…"}, "message": "…"}
}
```

`result` is the body the action's own route answers (an item, a run, a
steering record, a gate or a repository with its `operation`; the epic run
with its `operation_id`). `still_waiting` says whether an entry under this id
is on the list — dismissed alerts left out — as the answer is written:
`false` after a retry, an approval or a dismissal, `true` after an
`undismiss` or a re-armed review wait. It is a reading, not a promise: a
gate whose landing fails is reopened and waits again.

| Problem                         | When                                                                                                                           |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `409 not_waiting`               | Nothing is waiting under the id (with `entry_id`): it was settled, or it waits again as a new entry. Read the list again       |
| `409 not_eligible`              | The entry does not offer the action now; `action` and `offered` say which it does                                              |
| `422 unknown_action`            | The name is no action at all; `actions` lists the ones there are                                                               |
| `403 forbidden`                 | The caller lacks the action's `capability` (or `runs:read`)                                                                    |
| `422 idempotency_key_required`  | No `Idempotency-Key`                                                                                                           |
| `422 invalid_request`           | `params` the action does not take, a missing `expected_revision` on `gate_approve`, a revision sent for an entry that has none |
| `409 stale_revision`            | The entry moved since it was read (with `revision`)                                                                            |
| `409 idempotency_conflict`      | The key already names another act on this entry (with `operation_id`)                                                          |
| anything the action's route has | `capability_unknown`, `already_in_progress`, `task_not_failed`, `run_ended`, … as that route                                   |

There is no WebSocket command for an act: the socket's `command` frame takes
the actions' own names (`gate.approve`, `item.retry`, …) as before.

### Hearing that an entry appeared or left

With `attention.act`, the chronology carries three durable events, so a
client — or a notification rule — no longer polls the list and compares:

- `attention.opened` when an entry appears on the default list (dismissed
  alerts left out);
- `attention.resolved` when it leaves: it was approved, retried, skipped,
  resumed, dismissed, deleted, or its work moved by itself;
- `attention.reminder` while it is still there: once it has been open
  `[attention] remind_after_s` (4 hours by default), and again every
  `remind_every_s` (a day) — see [Still waiting](#still-waiting) below.

An `undismiss` opens the entry again under the same id; work that fails
again is a new entry and a new `attention.opened`. `data` is the same in
the first two:

```json
{
  "entry_id": "item:itm_x1:blocked:run_r1",
  "kind": "item", "group": "failed", "state": "blocked",
  "title": "…", "since": "…",
  "repository": "owner/name", "repository_id": "repo_…",
  "item_id": "itm_x1", "run_id": "run_r1", "gate_id": null,
  "plan_id": null, "node_id": null, "epic_run_id": null,
  "revision": 4
}
```

`entry_id` is exactly the list's `id`, and the other fields are the entry's
as the list had them when it opened (a `resolved` event repeats them: the
entry is no longer there to read). `channel_id` and `actions` are not in the
event — they depend on who is reading; read the entry for them. The event's
own `run_id` and `item_id` are set where the entry has them.

- **Who sees them.** They are scoped
  [as a run's events are](#who-sees-which-events): an entry about work a
  channel asked for reaches that channel's readers; work no channel asked for
  reaches workspace owners and admins; a plain API client sees all of them.
  An entry with no run and no item — a suspended repository, a provider hold,
  a task that was never admitted — is recorded with neither, and such an
  event reaches every workspace member, as the daemon's own notices about the
  same thing do. The list itself stays workspace-wide.
- **When.** The daemon compares the list with what it last announced when it
  records something that could have changed it (a command, a run starting or
  ending, a gate, a notice, a plan or an epic run moving) — within about a
  second — and otherwise once a minute, which is how a change that records
  nothing (polling that stopped, a provider hold) is found. Treat the events
  as prompt, not instantaneous; the list is always the truth.
- **Across a restart.** What was announced is kept, so a restart announces
  nothing twice and still reports what settled while the daemon was down.
- **On upgrade.** The first comparison records what is already waiting
  without announcing it: there is no burst of `attention.opened` for old
  entries. Their `attention.resolved` is still recorded when they leave.

#### Still waiting

An entry never expires to yes or to no: a merge gate, a publish hold, a
plan's questions or a blocked run waits until someone acts. So that it does
not wait in silence, the daemon records `attention.reminder` for an entry
that has been on the default list at least `[attention] remind_after_s` and
has not been reminded about within `remind_every_s`. Its `data` is the
opening's, plus:

```json
{
  "…": "the fields of attention.opened, as the list has them now",
  "waiting_s": 14400,
  "reminders": 1,
  "capabilities": ["gates:approve", "runs:control"],
  "actions": ["gate_approve", "cancel"]
}
```

- `waiting_s` is how long the entry has been announced (whole seconds);
  `since` still says when the thing itself started waiting.
- `reminders` counts the reminders sent for this entry, this one included.
- `capabilities` are the ones the entry's actions need, as
  [`GET /v1/attention`](#what-is-waiting-on-a-person) lists them, without
  the per-reader `allowed`: who could act. An entry with no action (a
  provider hold) has `[]`.
- `actions` are the entry's actions by name, in the list's order, without
  `allowed`; a push of the reminder offers each recipient the ones their
  role may take. A reminder recorded before this field reads as offering
  none.

It is scoped as the opening was (the same `run_id` and `item_id`, so the
same people see it). An entry that leaves the list and comes back — a
dismissal taken back, work that fails again under a new id — starts its
clock over. A restart repeats nothing and resets nothing: a daemon that was
down past several intervals records one reminder on return, not one per
interval missed. `remind_after_s = 0` records none. Reminders are judged on
the same passes that compare the list (within a minute), so a reminder is
due on the minute, not the second. A dismissed entry is off the default
list and gets none.

## Delegation

When `/v1/capabilities` lists `delegation`, an owner can state once that an
agent may take an action under conditions — a **grant** — and read the ledger
of what was decided under the grants. Grants do not ship empty: where
`/v1/capabilities` lists `delegation.defaults`, every installation, fresh or
upgraded, starts with Lantern's default grants, enabled (see "Default grants"
below). Where `planning.driver` is listed too, the daemon judges each step of a plan
whose `advance` is `auto` against them (see "Plans that advance themselves"
under [Plans](#plans)), and where `goals.proposing` is listed and
`[delegation] propose_every` is set, whether the planner may draft a plan
from a goal (`plan.propose`; "Plans proposed from goals" there).

A grant never widens what an agent's principal holds. An agent acting for
itself still carries `items:create` and nothing else; a grant is a rule the
daemon consults when *it* is about to act for that agent, with the resource in
hand.

**What can be delegated** is a closed list. A grant names exactly one of:

| Action             | The act                                               |
| ------------------ | ----------------------------------------------------- |
| `plan.propose`     | Draft a plan and queue the run that proposes its root |
| `plan.breakdown`   | Queue the run that proposes a node's next level       |
| `plan.approve`     | Approve a node's proposed children                    |
| `plan.publish`     | Publish an approved level to the forge                |
| `plan.run`         | Start an epic run                                     |
| `plan.run.retry`   | Retry a failed task of an epic run                    |
| `item.retry`       | Retry a failed work item                              |
| `run.grant_rounds` | Give an exhausted run more review rounds              |

Nothing else can be granted, and that is what keeps the rest with people:
writing or editing grants, credentials, daemon management (holds, stop,
restart, schedules, repositories) and configuration are not on the list, so no
grant can hand them to an agent.

**Conditions** are six optional keys, never an expression. One left out (or
`null`; `false` for `require_review`) constrains nothing. Each action accepts
only the keys that mean something for it; one that does not apply is refused
when the grant is written.

| Action             | `repositories` | `levels` | `max_children` | `require_review` | `causes` | `max_retries` |
| ------------------ | -------------- | -------- | -------------- | ---------------- | -------- | ------------- |
| `plan.propose`     | yes            | yes      |                |                  |          |               |
| `plan.breakdown`   | yes            | yes      |                |                  |          |               |
| `plan.approve`     | yes            | yes      | yes            | yes              |          |               |
| `plan.publish`     | yes            | yes      | yes            | yes              |          |               |
| `plan.run`         | yes            |          | yes            |                  |          |               |
| `plan.run.retry`   | yes            |          |                |                  | yes      | yes           |
| `item.retry`       | yes            |          |                |                  | yes      | yes           |
| `run.grant_rounds` | yes            |          |                |                  | yes      | yes           |

| Key              | Value                                  | Holds when                                                 |
| ---------------- | -------------------------------------- | ---------------------------------------------------------- |
| `repositories`   | a list of `owner/name`                 | the act's repository is one of them (case is ignored)      |
| `levels`         | a list of `initiative`, `epic`, `task` | the plan level the act is about is one of them (see below) |
| `max_children`   | a whole number, 1 or more              | the level has at most that many children                   |
| `require_review` | `true`                                 | the level's stored review verdict is `approve`             |
| `causes`         | a list of failure-cause names          | the failure's cause is one of them (see below)             |
| `max_retries`    | a whole number, 1 or more              | fewer retries than that were already made                  |

**Failure causes.** When `/v1/capabilities` lists `delegation.triage`, the
daemon's `operator` agent picks failures back up under its grants
(`item.retry`, `run.grant_rounds`, `plan.run.retry`), and `failure_cause` is
one of a closed set the daemon derives from the run's state, the budget it
exhausted and its tasks first, and from the recorded reason only as a last
resort:

| Cause                     | What it means                                                                        |
| ------------------------- | ------------------------------------------------------------------------------------ |
| `ci_timeout`              | CI or the landing did not settle within `[landing] ci_timeout_s`                     |
| `provider_throttle`       | the model provider held the run, or answered with a rate or usage limit              |
| `sandbox_resource`        | the sandbox ran out of disk or memory                                                |
| `forge_transient`         | the forge answered a 5xx, or the network to it failed                                |
| `verify_failed`           | a task's verify commands failed                                                      |
| `review_rounds_exhausted` | the run spent every review fix round it had                                          |
| `ci_rounds_exhausted`     | the run spent every CI fix round it had                                              |
| `merge_conflict`          | the pull request conflicts with its base                                             |
| `needs_person`            | the run stopped at something only a person can settle (an approval, a permission, …) |
| `unknown`                 | nothing recognisable                                                                 |

A grant's `causes` may list any of the first eight; `needs_person` and
`unknown` always go to a person and are refused in a grant, as is any other
name. `run.grant_rounds` is the act for the two exhausted causes (it grants two
more rounds on the same pull request); `plan.run.retry` for an epic run's
task; `item.retry` for any other failed or blocked item. `retries` is how many
times the ledger says that act was already allowed on that target, and triage
never takes it more than three times on one target whatever `max_retries`
says. Each act is an operation whose actor is the agent (`"kind": "agent"`,
`"id": "agent:operator"`), and its decision names the `item_id`, `run_id` or
`epic_run_id` and task `node_id` it was about. An escalation is written once
per situation and is resolved `acted` when the work is under way again,
`declined` when a person dismissed or abandoned it, and `superseded` when it
moved on otherwise.

For the plan steps, the level an act is about is the level it proposes,
approves, publishes or runs: an epic's breakdown, the approval and the
publishing of its tasks and its epic run are all `task`; an initiative's
are `epic`. `child_count` is how many children the level has (an epic
run's: its tasks on the forge), and `proposer` the agent or person every
child being approved was proposed by — left out when they differ or one is
not recorded, which escalates.

**Three outcomes.** Every act the daemon considers taking for an agent is
judged against the grants from facts the host established (never from what the
agent says about itself), and the answer is one of:

- `allow` — an enabled grant for that agent and action covers it: every
  condition holds and its `daily_limit` is not spent. When several do, the
  oldest (by `created_at`, then id) is the one named.
- `deny` — the action is not on the closed list; or it is `plan.approve` and
  the agent is the one that proposed the level. An agent never approves its
  own proposal, whatever the grants say.
- `escalate` — a person decides. No enabled grant names the agent and action;
  or a condition is not met (the reason names it and the value); or a fact a
  condition needs is missing or unreadable (the reason names what was needed —
  "could not tell" is never treated as met); or the grant's `daily_limit` is
  spent. An escalation waits: it never turns into a yes or a no by itself.

The cap day a `daily_limit` counts in is the one `[daemon] run_cap_timezone`
defines, the same day the run cap uses. `used_today` on a grant is counted from
the ledger's `allow` rows, not kept on the grant.

`GET /v1/grants` and `GET /v1/grants/{id}` (`audit:read`) return grants. The
list has Lantern's defaults first, in the order of the table below, then the
grants owners wrote, oldest first (the judge does not read this order; it
picks the oldest grant that allows an act):

```json
{
  "id": "grant_…",
  "workspace_id": "local",
  "agent_slug": "critic",
  "action": "plan.approve",
  "conditions": {
    "repositories": ["acme/shop"],
    "levels": null,
    "max_children": 8,
    "require_review": true,
    "causes": null,
    "max_retries": null
  },
  "daily_limit": 5,
  "used_today": 0,
  "enabled": true,
  "note": "small reviewed levels",
  "created_by": "cli_…",
  "created_by_display": "olive",
  "created_at": "…",
  "updated_at": "…",
  "revision": 1,
  "source": "owner",
  "default_key": null
}
```

`source` is `default` for one of Lantern's default grants and `owner` for one
a person wrote; `default_key` names which default it is (`null` on an owner's
grant). A default is edited, paused and deleted like any other grant, and
keeps its `source` and `default_key` through every edit.

`POST /v1/grants` (`policy:manage`) takes `agent_slug`, `action`, and
optionally `conditions`, `daily_limit` (`null` or absent is unlimited),
`enabled` (true when absent) and `note` (at most 500 characters), and answers
`201` with `{"grant": …, "message": "…", "operation": …}`.
`PATCH /v1/grants/{id}` takes `expected_revision` and any of `conditions` (the
whole set is replaced), `daily_limit`, `enabled` and `note`; only the fields
sent change, `409 stale_revision` (with `current_revision`) when the grant was
edited since it was read. A grant's agent and action are its identity and are
not edited — the ledger's rows name the grant — so changing either is a delete
and a new grant. `DELETE /v1/grants/{id}` removes one (`"grant": null` in the
reply); the decisions it allowed stay in the ledger. Each takes an optional
`Idempotency-Key`.

A write is refused with `422` naming the field:

- `invalid_argument` with `"field"` — `action` is not on the closed list;
  `conditions.<key>` does not apply to the action or holds a value that makes
  no sense; `agent_slug` names no agent the registry knows, names one by an
  alias, or names one that is disabled or archived (checked when a grant is
  created and again when a disabled grant is switched on); `daily_limit` is
  not a positive whole number.
- `invalid_request` with `"errors"` (each with its `loc`) — the body itself
  is malformed: an unknown key, a wrong type, a number below 1.

The write routes ask for `policy:manage` as a capability and never for a
role, so an admin is refused (`403 forbidden`, `"capability": "policy:manage"`), and so is a plain API client that counts as an owner
elsewhere because it holds `daemon:manage`. Each write is one operation
(`grant.create`, `grant.update`, `grant.delete`, target kind `grant`) with its
`operation.*` events, and the daemon narrates it as a `daemon.notice`
(`daemon.grant_added`, `daemon.grant_updated`, `daemon.grant_removed`), the
same two records a schedule write leaves. A write a restart interrupted is
settled from the stored grant at recovery — it is there, it holds the change,
or it is gone — and is never left `reconciling`.

**Default grants (`delegation.defaults`).** When the daemon starts it seeds
each default below that was never seeded on this installation, enabled, as
`created_by: "lantern"`, `created_by_display: "Lantern default"`, with a `note`
saying what it is for. Seeding writes no event of its own (the daemon logs
it); the grants say what they are wherever they are listed. A default is seeded once, ever: one an owner
deleted is not seeded again at the next start, and one an owner edited or
paused is never touched. There is no default token budget —
`[daemon] daily_token_budget` stays unset — and each default's `daily_limit`
is its spend guard.

| `default_key`                  | Agent      | Action             | Conditions                                                                   | `daily_limit` |
| ------------------------------ | ---------- | ------------------ | ---------------------------------------------------------------------------- | ------------- |
| `plan.breakdown:planner:v1`    | `planner`  | `plan.breakdown`   | `levels: [epic, task]`                                                       | 10            |
| `plan.approve:critic:v1`       | `critic`   | `plan.approve`     | `require_review: true`, `max_children: 8`                                    | 5             |
| `plan.publish:critic:v1`       | `critic`   | `plan.publish`     | `require_review: true`, `max_children: 8`                                    | 5             |
| `plan.run:critic:v1`           | `critic`   | `plan.run`         | `max_children: 12`                                                           | 3             |
| `plan.propose:planner:v1`      | `planner`  | `plan.propose`     | none                                                                         | 2             |
| `item.retry:operator:v1`       | `operator` | `item.retry`       | `causes: [ci_timeout, forge_transient, provider_throttle]`, `max_retries: 1` | 5             |
| `run.grant_rounds:operator:v1` | `operator` | `run.grant_rounds` | `causes: [review_rounds_exhausted, ci_rounds_exhausted]`, `max_retries: 1`   | 3             |

What they let happen: the plan defaults act only on a plan a person set to
`advance: auto`, and `plan.propose` also needs `[delegation] propose_every`
and an active goal — a `manual` plan, and every `code`, `workload` and `tool`
run, is untouched by them. The two `operator` defaults act as soon as the
daemon runs: triage retries an item that failed in the last day with one of
the three transient causes once (at most five a day), and gives a run that
spent its review or CI rounds two more rounds once (at most three a day); a
recent failure they do not cover is one `escalate` row. A
default whose agent is disabled or archived waits, unseeded, for a start where
its agent can act.

`POST /v1/grants/defaults/restore` (`policy:manage`, optional
`Idempotency-Key`, no body) writes again each default whose grant no longer
exists, as seeded; a default still there — edited, paused or as seeded — is
left alone. It answers `200` with the grants it wrote, in the table's order
(an empty list when every default is in place):

```json
{"grants": [{"id": "grant_…", "source": "default", "default_key": "item.retry:operator:v1", …}], "message": "1 default grant(s) restored: …", "operation": {"action": "grant.restore_defaults", "target": {"kind": "grant", "id": "defaults"}, …}}
```

It is one operation, `grant.restore_defaults` (target `grant` `defaults`),
narrated as a `daemon.notice` of kind `daemon.grants_restored`; one a restart
interrupted is settled at recovery from whether every default is in place.
An admin, a member and a plain client holding `daemon:manage` are refused
`403` naming `policy:manage`.

Grants are edited here and nowhere else: there is no `command` for them on the
WebSocket, no chat tool and no `ctl` verb. An owner's chat turn carries
`policy:manage`, and editing policy from a conversation is deliberately not
offered.

`GET /v1/decisions` (`audit:read`) is the ledger, newest first, paged by
`limit` and `cursor`. Filters: `outcome` (`allow`, `deny`, `escalate`),
`unresolved=true` (escalations still waiting for a person), `agent` (a slug)
and `since` (RFC 3339 or epoch seconds).

```json
{
  "id": "dec_…",
  "workspace_id": "local",
  "grant_id": "grant_…",
  "agent_slug": "critic",
  "action": "plan.approve",
  "outcome": "allow",
  "reason": "grant grant_… lets critic take plan.approve",
  "plan_id": "plan_…",
  "node_id": "node_…",
  "item_id": null,
  "run_id": null,
  "epic_run_id": null,
  "repository": "acme/shop",
  "operation_id": "op_…",
  "attrs": {"repository": "acme/shop", "level": "epic", "child_count": 4, "proposer": "agent:planner", "review_verdict": "approve", "level_digest": "r1-…"},
  "at": "…",
  "resolved_at": null,
  "resolved_by": null,
  "resolution": null
}
```

`grant_id` is set only on an `allow`. `attrs` are the facts the act was judged
on, kept for audit; the plan driver adds `level_digest` (the level as it
read when judged — a new digest is a new situation) and, when the children
disagree on who proposed them, `proposers`. An `escalate` row carries `resolved_at`, `resolved_by` and
`resolution` once it ends: `acted` (the step happened, whoever took it),
`declined` (a person said no) or `superseded` (what it was about changed).

## Goals

When `/v1/capabilities` lists `goals`, an owner or an admin can set a
**goal** for a repository: a standing objective, in their own words, that
plans are proposed from. `goals` is served with `planning`: a goal is for a
repository that can hold a plan. Goals ship empty, and nothing in the daemon
proposes a plan from one yet: this release stores and reads them.

`GET /v1/goals` (`runs:read`) lists every goal, oldest first, narrowed by
`repository` (case is ignored) and `state` (`active`, `paused`, `done`).
`GET /v1/goals/{id}` (`runs:read`) returns one:

```json
{
  "id": "goal_…",
  "workspace_id": "local",
  "repository": "acme/shop",
  "title": "Faster builds",
  "text": "Cut the build time in half without dropping a check.",
  "state": "active",
  "created_by": "usr_…",
  "created_by_display": "olive",
  "created_at": "…",
  "updated_at": "…",
  "revision": 1,
  "plans": [
    {"plan_id": "plan_…", "title": "Build pipeline", "state": "published", "advance": "auto"}
  ],
  "open_plan_id": "plan_…"
}
```

`plans` are the plans proposed from the goal (those whose `goal_id` names
it), most recently changed first, each with its root's `title`, the `state` a
plan reads as (`draft`, `published`, `archived`) and its `advance`.
`open_plan_id` is the plan currently serving the goal — the most recently
changed one that is not archived — or `null`.

`POST /v1/goals` (`plans:publish`) takes `repository`, `title` (1–200
characters), `text` (1–4000 characters, the objective) and optionally `state`
(`active` when absent), and answers `201` with
`{"goal": …, "message": "…", "operation": …}`. `PATCH /v1/goals/{id}` takes
`expected_revision` and any of `title`, `text` and `state`; only the fields
sent change, `409 stale_revision` (with `current_revision`) when the goal was
edited since it was read. A goal's repository is not edited. `DELETE /v1/goals/{id}` removes one (`"goal": null` in the reply); the plans
proposed from it keep their `goal_id`. Each takes an optional
`Idempotency-Key`.

A write is refused with `422` naming the field: `invalid_argument` with
`"field": "repository"` when the repository is not configured on this server,
is disabled, or cannot hold a plan (the detail says which); with
`"field": "title"` or `"text"` when one is blank; `invalid_request` with
`"errors"` when the body is malformed (an unknown key, an overlong title, a
state outside the three).

The write routes ask for `plans:publish`: an owner or an admin sets direction,
a member is refused (`403 forbidden`, `"capability": "plans:publish"`). Each
write is one operation (`goal.create`, `goal.update`, `goal.delete`, target
kind `goal`) with its `operation.*` events; a write a restart interrupted is
settled from the stored goal at recovery. A plan's `goal_id` is set by the
daemon and is not accepted on the plan routes. There is no `command` for goals
on the WebSocket, no chat tool and no `ctl` verb.

## Fleet analytics

When `/v1/capabilities` lists `analytics`, `GET /v1/analytics` (`runs:read`)
answers "is this performing well" for a window of runs: the same fold the
console's Overview draws, with every derived value as a field so a client
never recomputes one. A run belongs whole to the window it **began** in.

| Query      | Default | Bounds                                                                               |
| ---------- | ------- | ------------------------------------------------------------------------------------ |
| `window_s` | 604800  | 60 to 7776000 (90 days)                                                              |
| `buckets`  | 7       | 1 to 90 equal slices of the window                                                   |
| `until`    | now     | RFC 3339 or epoch seconds: where the window ends; the window begins in 1970 or later |

A value outside these is `422 invalid_request`. The response:

- `since`, `until`, `observed_at`, `window_s`, and `empty` (no run began in
  the window).
- `total` and `lanes` (one per run kind, by name): `runs`, `landed` (merged
  or completed), `failed`, `cancelled`, `turns`, `tokens` (input plus
  output), `cache_read_tokens`, `active_s` (the time phase attempts were
  running), `elapsed_s` (creation to last update), `parked_s` (elapsed the
  loop did not spend working — waiting on a person), `ok_rate` and
  `parked_share`. A cancelled run is a decision, not a failure: `ok_rate` is
  landed over landed plus failed, and `null` when no run was judged.
- `phases`: per phase, `attempts`, `retries` (attempts past the first),
  `turns`, `tokens`, `cache_read_tokens` and `active_s`, longest first.
- `buckets`: each with its own `since` and `until`, the `runs` that began in
  it, how many of them `landed`, `failed` or were `cancelled`, and their
  `turns`.
- `rework` (`tasks`, `revisions`, `replans`, `suspect`, `retried_share`),
  `review_rounds` and `ci_rounds`.
- `failures`: `reason` and `count`, most common first. The reason is the head
  of the failed runs' own reason — the class, not one run's detail.
- `costliest` (most turns) and `longest_parked`: up to eight runs each, by
  `run_…` id, with `kind`, `state`, `turns`, `tokens`, `active_s` and
  `parked_s`.
- `spreads`: `median` and `p90` for `turns`, `cycle_s` (creation to last
  update over the runs that landed — time to land) and `active_s`; `null`
  where no run gives one.
- `previous` (the window before this one, every kind together; `null` when no
  run began in it) and `delta`: each of the lane's values as a share of the
  previous window's (`0.25` is a quarter more), `null` when there is nothing
  to compare with — a change from nothing is not a percentage.

Durations are seconds. Nothing here is a currency: turns and tokens are what
a backend reported, not a bill.

## The briefing

When `/v1/capabilities` lists `briefing`, `GET /v1/briefing` (`runs:read`)
answers, in one request, what a person who has been away asks first: what
happened, what needs me, and is there work lined up. Everything in it is on
its own route already — the analytics, the attention list, the decisions
ledger, the usage pool, the plans, the queue — and the briefing is the small
summary a landing screen, a phone widget and a daily digest share, computed
on read in a bounded number of statements so it can be polled. Nothing is
stored, and reading it changes nothing.

| Query   | Default   | Meaning                                                                              |
| ------- | --------- | ------------------------------------------------------------------------------------ |
| `since` | a day ago | RFC 3339 or epoch seconds: where the window begins. At most 90 days back, before now |

A value outside those bounds, or that is not a time, is `422 invalid_request`.
The window ends now. The response:

- `since`, `until`, `observed_at`.
- `outcomes`: the runs that **finished** inside the window — `landed` (merged
  or completed), `failed` and `cancelled`, in total and `by_kind` (one entry
  per run kind, by name), and `recent_landed`: the newest ten landed runs,
  each with `run_id`, `kind`, `title` (the work item's; the run's own ask
  when no item carries it), `repository`, `pull_request_number` and
  `pull_request_url` where there is one, and `landed_at`. A run is in the
  window when it *finished* in it, whenever it began — this is not the
  analytics' window, which holds the runs that *began* in it. A `blocked`
  run is not an outcome: it waits, and is counted under `waiting`. A deleted
  run is counted and never listed.
- `waiting`: the attention list's own `counts` (`total`, `decision`,
  `failed`, `paused`) and `oldest_since`, when its longest wait began
  (`null` when nothing waits). Exactly what
  [`GET /v1/attention`](#what-is-waiting-on-a-person) would answer.
- `decided`: what agents decided under grants inside the window, by outcome
  (`allow`, `deny`, `escalate`), and `unresolved_escalations` — every
  escalation still waiting for a person, however old. `recent` is the
  allowed acts, newest first and at most ten, each with `id`, `grant_id`,
  `agent_slug`, `action`, `reason`, `at` and the references
  [`GET /v1/decisions`](#delegation) carries (`plan_id`, `node_id`,
  `item_id`, `run_id`, `epic_run_id`, `repository`, `operation_id`). It is
  filled only for a caller holding `audit:read` and is `null` for anyone
  else: the counts are for everyone, the detail is not.
- `supply`: how much work is lined up, as it stands now. `proposed` and
  `approved` are plan nodes at any level in those states across the plans
  that are not archived (awaiting a person's approval; approved and not yet
  published). `ready_tasks` are the published tasks ready to start: on the
  forge and still following their issue, the issue open as last reconciled,
  and not started — no epic run has admitted the task (`queued`, `running`),
  seen its item end (`landed`, `failed`), found its issue `closed` or had a
  person `skipped` it; a task `waiting`, `ready`, `blocked` or withdrawn
  (`cancelled`) before admission is still lined up. A task whose issue a
  person labelled by hand, outside any epic run, is counted until its issue
  closes. `queued` is the daemon queue's depth, `running` the runs in
  flight, and `parked` the work parked on a person (the attention list's
  `decision` plus `paused`).
- `runway`: `ready_tasks` again, `landed_per_day` — the mean number of `code`
  runs that landed per day over the trailing seven days, whatever `since`
  was — and `days`, `ready_tasks` divided by that rate. Both are `null` when
  no `code` run landed in those seven days: no rate is invented, and nothing
  is divided by zero.
- `budget`: `runs_today` against `max_runs_per_day` and `tokens_today`
  against `daily_token_budget` (`null` when no budget is configured), with
  `resets_at` — the figures of
  `GET /v1/usage/pool`, for the pool's calendar day.
- `grants`: how many grants are `enabled`, and how many of those are
  `at_limit`, having allowed as many acts today as their `daily_limit`.

Every field is always present; what cannot be said is `null`. Durations are
seconds, timestamps RFC 3339, and nothing is a currency. Each part is its own
object so a later release can add a field inside it: **a client ignores
fields it does not know** rather than failing the page.

### The daily digest

With `[attention] digest_at` set (a local time of day, `"HH:MM"`, in
`[daemon] run_cap_timezone`; off by default), the daemon computes this
briefing once a day by itself, at or after that time, for the window since
the previous digest (a day, the first time) — with the same code, as the
summary anyone may read (no `decided.recent`) — and records one
`briefing.digest` event:

```json
{
  "type": "briefing.digest",
  "run_id": null,
  "item_id": null,
  "data": {
    "day": "2026-10-02",
    "since": "2026-10-01T07:00:00Z",
    "until": "2026-10-02T07:00:00Z",
    "landed": 11,
    "failed": 1,
    "waiting": 2,
    "decided_allow": 9,
    "decided_escalate": 0,
    "runway_days": 2.5,
    "timezone": "UTC"
  }
}
```

`landed` and `failed` are `outcomes`, `waiting` is `waiting.total`,
`decided_allow` and `decided_escalate` are `decided.allow` and
`decided.escalate`, and `runway_days` is `runway.days` (`null` without a
rate). The numbers only: no title, no reason, nothing of what was decided.
It names no run, item or channel, so **every member** sees it, whatever
their role. The same summary goes to the control channel as one
`daemon.notice` (`kind: "daemon.digest"`, `level: "info"`) and, with push
on, to every member as [one `work` push](#push-notifications). One a day:
a restart repeats nothing, a daemon that was down at the time sends it once
when it is back the same day, and a day missed entirely is skipped.

## Errors

Every refusal is `application/problem+json` with a stable `code`, the
request's `X-Request-Id`, and the fields a client needs to act:

| Status | Codes                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 400    | `invalid_request`, `invalid_cursor`, `oidc_invalid_request`                                                                                                                                                                                                                                                                                                                                                               |
| 401    | `unauthenticated`, `invalid_token`, `token_expired`, `token_revoked`, `client_revoked`, `refresh_reuse_detected`, `oidc_exchange_failed`                                                                                                                                                                                                                                                                                  |
| 403    | `forbidden` (with `capability`), `agent_forbidden`, `oidc_not_allowed`, `oidc_account_disabled`, `oidc_not_provisioned`                                                                                                                                                                                                                                                                                                   |
| 404    | `not_found`, `unknown_target`, `agent_not_found`, `device_not_found`, `notification_not_found`                                                                                                                                                                                                                                                                                                                            |
| 409    | `not_eligible`, `not_waiting`, `already_terminal`, `already_in_progress`, `stale_revision`, `unsupported_for_kind`, `capability_unknown`, `capability_unsupported`, `idempotency_conflict`, `hold_owned`, `unsupervised`, `agent_read_only`, `agent_revision_conflict` (with `current_revision`), `agent_exists`, `agent_archived`, `oidc_account_conflict`, `device_limit_reached` (with `limit`), `device_not_enrolled` |
| 410    | `cursor_expired` (with `snapshot`), `artifact_gone`                                                                                                                                                                                                                                                                                                                                                                       |
| 411    | `length_required`                                                                                                                                                                                                                                                                                                                                                                                                         |
| 413    | `body_too_large` (with `limit`)                                                                                                                                                                                                                                                                                                                                                                                           |
| 422    | `invalid_request` (with `errors`), `invalid_argument`, `idempotency_key_required`, `unknown_action`, `invalid_agent` (with `problems`)                                                                                                                                                                                                                                                                                    |
| 429    | `too_many_attempts`, `too_many_streams`                                                                                                                                                                                                                                                                                                                                                                                   |
| 500    | `internal_error` (never the exception's text)                                                                                                                                                                                                                                                                                                                                                                             |
| 502    | `push_relay_refused`, `push_relay_unavailable`                                                                                                                                                                                                                                                                                                                                                                            |
| 503    | `daemon_not_ready` (with `Retry-After`), `daemon_stopping`, `source_unavailable`, `oidc_unavailable`, `push_disabled`                                                                                                                                                                                                                                                                                                     |

`unknown_target` and `not_eligible` carry the daemon's own sentence in
`detail` — the same one `ctl` prints.

## Limits

| Limit                          | Value                                              |
| ------------------------------ | -------------------------------------------------- |
| Page size                      | 50 default, 200 maximum                            |
| Request body                   | `[api] max_body_bytes` (256 KiB)                   |
| Live streams (SSE + WebSocket) | `[api] max_stream_clients` (32)                    |
| Concurrent artifact downloads  | 4                                                  |
| Artifact catalog per run       | 2000 files                                         |
| Log tail                       | 500 records                                        |
| Usage window                   | 90 days                                            |
| Analytics window               | 1 minute to 90 days, in at most 90 buckets         |
| Access token                   | `[api] access_token_ttl_s` (15 minutes)            |
| Refresh token                  | `[api] refresh_token_ttl_s` (7 days)               |
| Auth failures                  | 10 per minute per client and address, 60 s lockout |
| Operation deadline             | `[api] operation_deadline_s` (5 minutes)           |
| WebSocket auth frame           | 5 s                                                |
| Store calls in flight          | 8 (4 executor threads)                             |

A stream's whole state is a cursor: a slow client blocks nobody, and the
daemon's stores are never touched from the event loop.

## Isolation

A worker sandbox can never reach the daemon's API. Two facts hold it:

- No allowlist the provisioner hands `sbx policy allow network` names the
  host, its loopback, or the address the listener binds — whatever the
  backend, the toolchains or the registries — and `[sandbox] extra_allow_domains`
  refuses a bare address, a loopback name, a container runtime's host
  alias and `*` (`tests/unit/test_api_isolation.py`).
- `lantern doctor --deep` probes it live: the `api-host-unreachable`
  conformance probe asks, from inside a scratch sandbox, for the API's
  `/health/live` answer on `[api] port` at the guest's loopback,
  `host.docker.internal`, its default gateway and the `[api] bind` address,
  both directly and through the sandbox's proxy, and asks the network
  policy about those addresses. Only the API's own answer counts as
  reachable: sbx accepts connections its policy then closes unanswered, so
  an opened connection proves nothing. Anything but `unreachable` fails the
  drift gate on CI runners.

**Field-unverified:** the probe's verdict against a real sbx release is
established by the CI runners that execute `doctor --deep`, never assumed
from a developer machine.

## Recovery procedures

- **The daemon restarted.** Holds stand with their owners; the queue is
  intact; a run in flight resumes through the daemon's own queue. Read
  `/health/ready` for the new `generation`, then `GET /v1/status` and
  subscribe from its watermark. Operations a previous generation left open
  are settled from evidence (`succeeded`, `failed interrupted_before_effect`,
  or `expired` for intent never claimed) — read them, never retry blindly.
- **A reply was lost.** Replay the request with the same `Idempotency-Key`:
  the answer is the operation that already exists, whatever became of it.
- **A stream dropped.** Reconnect from the last `evt_<n>` you processed.
  On `410 cursor_expired`, read a fresh snapshot and subscribe from its
  watermark; what you missed is in the resources themselves.
- **A token stopped working.** `401 token_expired`: refresh. `401 client_revoked`: the operator revoked the client; work already admitted
  stands. `401 refresh_reuse_detected`: the family was revoked, its live access
  tokens with it; mint from the secret and treat the reuse as a leak.
- **A hold you did not take blocks the queue.** `GET /v1/daemon/holds`
  names its owner; release it with `?force=true` only as an override, which
  the record shows as yours.
- **The daemon is down.** `GET /health/live` refuses the connection;
  starting it is the supervisor's job, not the API's. A `POST /v1/daemon/stop`
  under a service manager that restarts the daemon is a restart, holds
  included.
- **Something disagrees with the record.** `GET /v1/operations` is the
  record every surface writes; `GET /v1/logs` and `GET /v1/configuration`
  say what the daemon runs on, with secrets and host paths redacted.

## What is not offered

By design, on this API: general configuration writes, backup and restore,
garbage collection, sandbox deletion, and starting a daemon that is not
running. Each stays on the host's own CLI until it has its own attribution,
conflict and active-run story. Deleting one finished piece of work has one
(see Deleting finished work above) and removes that work's own run
directories and sandboxes; the retention sweep and every other sandbox are
still the host's. Repository registration has one (see
Repositories above); a repository's other settings are still the file's. A tool run takes no
steering, no round grants and no gate: a fixed recipe has nothing to steer.
Grants are written through `/v1/grants` only (see Delegation above): not from
the WebSocket's commands and not from a conversation.

## Readiness criteria for a hosted service

The single-installation loop is complete when every scenario in
`tests/api/conformance/` passes on CI, the OpenAPI snapshot is committed
and unchanged by the build, and `doctor --deep` reports `api-host-unreachable`
as `unreachable` on a CI runner. Proceeding to a hosted, multi-tenant service
(the spike's stages four and five) additionally requires, before any
customer data enters:

1. **Identity.** A hosted identity provider behind the same capability
   model; `workspace_id` varying per tenant with every id, cursor, nested
   reference and download checked against it (today `"local"` everywhere).
2. **Execution routing.** One daemon per tenant home, or a scheduler that
   proves tenant isolation of sandboxes, stores and secrets — never a shared
   store.
3. **Credentials.** BYOK bindings per provider and backend with observed
   billing attribution; secrets never in events, logs or argv, as here.
4. **Durability.** A backup, restore and retention policy for the operation
   record and the chronology, with the upgrade and rollback limits published
   before two client versions read one store.
5. **Operations.** Request latency and error rates, pending-operation age,
   reconciliation backlog, projection lag and disconnected-consumer counts
   exported from the daemon; the limits above enforced per tenant.
6. **Field verification.** The isolation probe and the listener's restart
   behaviour driven on the target platform, and every remaining
   field-unverified note in the stack's pull requests closed.
