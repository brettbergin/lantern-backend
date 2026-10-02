# Work channels: one unit of work, one conversation, every surface

Status: decided 2026-10-01; implemented by the pull requests this document
names at the end.

## The problem

Lantern had two chat architectures that showed the same run in two shapes.

The chat bridges — Discord, Slack, Mattermost and the operator console's
local bridge — post a headline per run in one *control channel* and open a
*thread per run* under it, streaming the chronology there. A person steers
by @mentioning the bot in that thread (`daemon/chat.py`, `_steer`), which
hands the text to the engine directly: no `runs:steer` check, no steering
record, no operation, nothing the other surfaces can see.

The collaboration API (`/v1/channels`), which the web and iOS apps speak,
has durable channels that hold *many* runs each, rendered as a folded run
card under the reply that queued it. A person steers in the card, or by
@mentioning an agent in the composer, which steers only when exactly one
live run in the channel has that agent. Work admitted outside chat (a
labelled issue, a schedule) already got a channel of its own
(`collaboration.external_work`), so the apps rendered two shapes with one
component.

`DaemonLoop` calls `frontend.run_started` for every run with no channel
filter, so a run a linked channel asked for existed twice: the bridge's own
headline and thread, and the channel's run posts mirrored to the linked
surface, sharing no identifier. `409 link_run_thread` kept the two apart.

## The decision

- **One unit: a work channel per job.** Every job — admitted from chat, a
  label, a schedule or the API — gets the system-created, workspace-visible
  channel external work already got. Attempts (retries) share it. The chat
  that asked for the work keeps a hand-off message naming the job, which a
  client shows as a status card linking to the work channel, and the
  delivery or notice the asking turn already received.
- **Steering is the work channel's composer.** A plain message in a work
  channel while its one run is live is direction for that run; `@agent`
  addresses that agent's lane; `/stop` stops. Every path lands in
  `ControlService.steer`, so one record, one `run.steer` event, one rule.
- **A bridge's run thread is a link of the work channel.** The headline and
  thread the bridges open stay; the thread is registered as a
  `ChannelLink` of the work channel, admitting guests. Outbound, the
  channel mirror carries the work channel into the thread; inbound, a reply
  in the thread is a turn in the work channel, so it steers through the
  same path as a reply typed in the app. The bridge keeps what the channel
  has no message for — the headline card, the status line and the tool
  digest edited in place, the agent's narration — and stops rendering what
  the channel already says (the task roster, verdicts, steering replies).
  The local bridge is a `ChatBridge`, so the console inherits all of it.
- **Anyone who can post in a bridge thread steers**, as before; the link
  admits guests. Stopping keeps the channel stop's rule (a member).
- **Advertised as `collaboration.work_channels`.** Older clients keep
  `/work` and `/jobs` as they were; a job's rows carry the work channel's
  id so a newer client sends a reader there instead of drawing a card.

## What was considered and refused

- *A channel per run.* A retry would start a new conversation and lose the
  attempt history the external-work design deliberately keeps together.
- *Feeding the bridge from the chronology instead of the run bus.* The bus
  and the chronology carry the same engine events; the duplicate was the
  rendered transcript, not the subscription. Dropping the lines the channel
  already posts removes it without rewriting the pump.
- *Moving the console to `/v1`.* Worthwhile, separate: the console reads
  `state.db` and the ctl queue, and the local bridge already gives it the
  work channel through the link.

## Pull requests

- lantern-backend: work channels, one steering path, bridge threads as links
  (`collaboration.work_channels`).
- lantern-web-app: the work-channel screen and the hand-off card.
- lantern-mobile-app: the same, with the parity rows it moves.
