# Work lives in the channel that asked for it

Status: decided 2026-10-02. It replaces the decision this file recorded on
2026-10-01 (every job in a channel of its own), which shipped in 2.1.44 and
was wrong; what it got wrong is below.

## The problem

Lantern had two chat architectures that showed the same run in two shapes.

The chat bridges — Discord, Slack, Mattermost and the operator console's
local bridge — post a headline per run in one *control channel* and open a
*thread per run* under it, streaming the chronology there. A person steered
by @mentioning the bot in that thread (`daemon/chat.py`, `_steer`), which
handed the text to the engine directly: no `runs:steer` check, no steering
record, no operation, nothing the other surfaces could see.

The collaboration API (`/v1/channels`), which the web and iOS apps speak,
has durable channels. A run was drawn as a folded card under the reply that
queued it and steered through a box inside that card, as if it were a
thread hanging off the conversation rather than the conversation's subject.

What was asked for: in Lantern's own surfaces a channel represents the
work, and people and agents talk and steer in the channel — not in a thread
under it.

## The first decision, and why it was wrong

The first answer gave every job a new, system-created *work channel* and
left a hand-off card in the chat that asked. That is the thread model with
a larger container: the conversation still forked away from where it began,
and one piece of work had two channels — the one it was asked in and the
one it ran in. The design questions that led there ("what does the asking
chat keep?", "what is the unit of a work channel?") presupposed the split
instead of asking whether the work should leave the channel at all.

## The decision

- **Work stays in the channel it was asked in.** A job admitted from a chat
  turn, or through the API naming a channel, is bound to that channel: its
  runs, their chronology and their delivery happen there. No second
  channel, no hand-off.
- **What comes next happens there too.** A retry, a resume, and a new ask
  once the last run has ended all run in the same channel.
- **One run at a time per channel, and the channel stays a conversation.**
  While a run is queued or running, a plain message in the channel goes to
  the model with the read tools and a `steer_run` tool for the live run: a
  question about the run ("how is it going?") is answered from its record,
  and a message that tells the run what to do differently is handed over
  (`ControlService.steer`: one record, one `run.steer` event, one rule,
  `steered_run_id` on the turn). `@agent` addresses that agent's lane, and
  `/stop` stops it. An explicit ask for *new* work — a turn that picks a
  runner, an admission naming the channel, an agent filing an issue to be
  run — cannot start until the run has ended: a turn is answered without
  the start tools, an admission is refused. The rule for admissions is
  `controls.intake.channel_refusal`, checked wherever work is admitted.
- **Work nobody asked for in a channel keeps a channel of its own.** A
  labelled issue, a schedule's tick and a bare API admission have no
  conversation to live in; they get the system-created, workspace-visible
  channel external work always had (`collaboration.external_work`).
- **A bridge's run thread is a link of the run's channel while the run is
  live.** The headline and thread the bridges open stay; the thread is
  registered as a `ChannelLink` of the channel, admitting guests, so the
  channel reaches the thread and a reply in the thread is a turn in the
  channel. The link is retired when the run ends, because the channel goes
  on to host other runs, each with a thread of its own.
- **Advertised as `collaboration.channel_runs`**, in place of
  `collaboration.work_channels`. A client without the new flag draws a run
  card in the chat with its own steer box, as it did before either.

## What was considered and refused

- *Queueing a second ask behind the live run.* The channel would then hold
  a waiting job beside the live one, and a plain message would have two
  things it might mean.
- *Treating an explicit new ask as steering.* A turn that picked a runner
  would silently become direction for a different run.
- *Treating every plain message as steering.* The first cut of this
  decision (2.1.48–2.1.50) did, and refused a runner turn before the model
  was asked, so nothing could be said in the channel while its run was
  live — not even "how is it going?". Undone: the model has the read tools
  and `steer_run`, and decides what is a question and what is direction.
- *Migrating the channels 2.1.44–2.1.47 made.* They and their hand-off
  messages stay readable; a job bound to one runs its next attempt in the
  channel that asks for it.
- *Giving the console channels.* It stays an operator's view of runs: what
  is typed in a run's screen reaches the run's channel through the link.
  Worth doing, and separate.

## Pull requests

- lantern-backend: bind work to the asking channel, one run at a time, the
  thread link retired at the end of its run (`collaboration.channel_runs`).
- lantern-web-app, lantern-mobile-app: the composer stays a conversation
  while the channel's run is live (its hint says the run is here and what
  the message can be), the run card keeps its own steer box, and the
  hand-off card and the separate work-channel screen go.
