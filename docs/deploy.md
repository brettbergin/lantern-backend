# Running the daemon as a service and upgrading it

This is the generic guide: install `lantern daemon` under systemd, upgrade it by hand, and
— optionally — let a GitHub Actions workflow on the host keep it current from lantern's
GitHub Releases. Nothing
here is specific to any one host or repository. How this repository deploys its *own*
daemon is a separate reference: [docs/self-deploy.md](self-deploy.md).

## The one rule: never restart under a live run

A restart kills the in-flight run's sandboxes. The run stays resumable and is picked up on
the next start, but the resume spends one of the item's resume budget and everything the
run had done since its last task boundary. Every upgrade path below therefore takes a named
pause hold, waits for the daemon to be idle, and only then installs and restarts.

`lantern daemon ctl status --json` is what scripts read for that. It prints one JSON object:

```json
{"current": null, "claiming": null, "holds": ["deploy-12"], "paused": true, "queued": 2, ...}
```

- `current` is the running item (`{item_id, run_id, title}`) or `null`.
- `claiming` is an item whose claim is in progress — busy for this purpose, so a restart is
  never timed into the window between the claim comment landing and the claim being
  persisted.
- `holds` is the set of named pause holds; `paused` is whether any stand. Holds are
  persisted: **a restart comes back with every hold still standing**, and `hold_details`
  says whose each one is. A hold ends only when its owner releases it or an operator runs
  `resume --all`.
- `source_failures` counts consecutive failed discovery polls, independently of failed
  runs. `source_retry_in_s` gives the remaining polling backoff. A successful poll resets
  both; plain status and the chat status card warn while polling is failing.

The prose `ctl status` is for people and may change; the JSON is for scripts. Exit codes:
`0` answered, `1` answered but pending or without a structured status (a daemon older than
this flag), `2` no daemon answered — the last one means there is nothing to drain. A
`status` call mutates the circuit breaker, so poll no faster than every 15 s.

The automated workflow runs `lantern doctor` before taking its hold or installing anything.
An existing Docker login or host failure stops deployment with the running install intact.
After rollback it checks the installed version, service, doctor and control response before
reporting success; a running process alone does not establish a successful rollback.

## Install

[contrib/systemd/README.md](../contrib/systemd/README.md) walks through it: one
`curl … | sh` (or `lantern init --systemd` from any existing install) builds the **Lantern
home**, `~/.lantern` (`LANTERN_HOME` moves it), and that home is what every upgrade path
assumes:

|                 |                                                                                                          |
| --------------- | -------------------------------------------------------------------------------------------------------- |
| Interpreter     | `~/.lantern/venv` — `lantern-backend[discord,slack]` and `lantern-worker`, uv-managed CPython            |
| Command         | `~/.lantern/bin/lantern` — the launcher; `~/.lantern/bin/sbx` wraps the home's sbx                       |
| Config, secrets | `~/.lantern/config/lantern.toml`, `config/secrets.env` (0600), `config/github-app.pem`                   |
| State, runs     | `~/.lantern/state/` (the SQLite store), `~/.lantern/runs/<run>/`, `~/.lantern/workspaces/<owner>/<name>` |
| Logs, backups   | `~/.lantern/logs/daemon.log`, `~/.lantern/backups/<stamp>/`                                              |
| Service         | `~/.lantern/systemd/*.service`, enabled into `~/.config/systemd/user` by init                            |
| Sandbox backend | user unit `sbx-sandboxd.service`, which the daemon unit `Requires=`                                      |

There is no working directory: every `lantern` command answers the same from anywhere.
Installing both chat extras makes `[chat] backend` a config change, not a reinstall.

## Host preparation, before the first unattended start

The install above puts everything under a directory the service account owns and needs no
root. Getting the *host* ready is a separate one-time job, and on Linux most of it is an
administrator's. Lantern reports each item and performs none of them — it joins no group,
installs no package, starts no sandbox and elevates nothing:

| Capability                         | Who                       | How it fails when missing                                                   |
| ---------------------------------- | ------------------------- | --------------------------------------------------------------------------- |
| `/dev/kvm` exists                  | administrator             | an `init` note and a `doctor` row; every sandbox boot fails later           |
| `/dev/kvm` openable by the account | administrator             | same row, different diagnosis — the usual cause is the `kvm` group          |
| `mkfs.ext4` (e2fsprogs)            | administrator             | `lantern init` refuses the sbx step by name, before downloading it          |
| sbx's AppArmor profile in `/etc`   | administrator             | `init` notes it and finishes; the sandbox backend will not start            |
| a reachable `systemctl --user`     | log in as the account     | `lantern init --systemd` refuses rather than enabling dead units            |
| lingering for the account          | account, or administrator | `init` reports persistence as **not confirmed**; the daemon stops at logout |

Two of these decide whether a deployment is unattended at all, so they are checked before
the units are written rather than discovered at the first logout:

- **The user service manager.** `systemctl --user` needs this account's own systemd
  manager and its `$XDG_RUNTIME_DIR`. A session that has neither — a bare `su`, a
  container, a CI step — takes every call with a bus error, and units "enabled" into a
  manager that was never reached are not a service. `init --systemd` fails there by name;
  log in as the account on the console or over ssh, or install with `--no-systemd` and
  start `lantern daemon` under whatever supervisor the host does use.
- **Lingering.** `loginctl enable-linger <account>` is what keeps user services running
  after logout. It can be refused outright (polkit does not always let an account enable
  its own) and it can be taken on a host where lingering still reads off, so `init` reads
  the state back from `loginctl` afterwards. Anything it cannot read is reported as *not
  confirmed* — never as set up. Check it any time with:

```bash
loginctl show-user "$USER" --property=Linger    # Linger=yes, or it is not unattended
lantern doctor                                  # the same, as a row, with everything else
```

`lantern doctor` shows the whole set on demand. The rows are diagnoses, not gates: they say
what is wrong and who has to fix it, and the sbx rows below them are what actually fail when
a sandbox cannot boot. Off Linux, and in a `--no-systemd` install, the rows that do not
apply are not shown, so a supported mode is never judged against a capability it never
wanted.

## The remote API behind a proxy

`[api] enabled = true` makes the daemon serve its remote API on `127.0.0.1:8420`.
It speaks plain HTTP and never terminates TLS itself: put a reverse proxy in front
(Caddy, nginx, your ingress), let it terminate TLS and forward to loopback, and
list the proxy's address in `[api] trusted_proxies` so the client address behind
`X-Forwarded-For` is the one the authentication limiter keys on. Nothing else on
the host needs to change: the listener runs inside `lantern daemon`, under the same
unit, and stops with it. `lantern api client create` registers a client and prints
its secret once; `lantern api key rotate` replaces the token signing key (restart
the daemon for the listener to sign with it — tokens signed by the old key still
verify until they expire).

## Upgrading by hand

Lantern is distributed through GitHub Releases only. Each release carries both wheels and
a `release-manifest.json` with their SHA-256; every upgrade path installs those files,
checked against that manifest, and never `lantern` or `lantern-worker` by name from a
package index. To move a home to the newest release, `lantern update` does the download,
check and install in one step. For an exact version, as the service user, once the daemon
is idle:

```bash
lantern daemon ctl pause --hold upgrade
until [ "$({ lantern daemon ctl status --json 2>/dev/null || echo '{}'; } | jq -r '.current // .claiming // "idle"')" = idle ]; do sleep 15; done

lantern backup --label pre-X.Y.Z
lantern init --no-sbx --version X.Y.Z   # fetch, check and install that release's wheels
lantern init --systemd --no-sbx         # the new version refreshes launchers and units
systemctl --user reset-failed lantern-daemon && systemctl --user restart lantern-daemon
```

`lantern backup` snapshots the config, secrets, units and the state database first (`backup list`,
`backup restore <name>`; the daily sweep keeps the newest `[daemon] backups_keep`). `init`
refreshes the launchers and the rendered units for the new version. `--no-sbx` preserves
the installed sandbox runtime; plain `init` would install this Lantern release's default
sbx version, which may be older than the one the operator installed.

On a host installed under a custom `LANTERN_HOME`, read `~/.lantern` above as that root —
`lantern` itself already honours the variable, so only the two explicit `~/.lantern/…`
paths change. `reset-failed` matters: `StartLimitBurst=5` per 600 s leaves a unit that
crash-looped in `failed`, where a plain `restart` will not revive it. The daemon comes back
with the `upgrade` hold still standing, so release it (`ctl resume --hold upgrade`) once
the checks below pass. Pin the version
exactly — a downgrade is the same two commands with an older `X.Y.Z`. Then check it:

```bash
systemctl --user is-active lantern-daemon
lantern --version
lantern doctor                       # never --deep or --probe here; those boot microVMs
lantern daemon ctl status --json     # exit 2 = no daemon came up
```

## Upgrading automatically

[contrib/workflows/deploy-daemon.yml.example](../contrib/workflows/deploy-daemon.yml.example)
is the by-hand procedure as a workflow, plus rollback. Copy it to
`.github/workflows/deploy-daemon.yml` in the repository that owns the host. It needs a
self-hosted Actions runner on the host, running as the service user, and takes **no**
checkout — it installs release wheels and needs nothing from the tree.

```
schedule / workflow_dispatch → self-hosted runner on the daemon host
                                ├─ compare the latest release (or the named version) with what is installed
                                ├─ fetch and check the target's and the installed version's wheels
                                ├─ take a named pause hold (deploy-<run id>)
                                ├─ wait — no cap — for the in-flight run to finish
                                ├─ snapshot, then install the exact version into the home's venv
                                ├─ restart the unit (the standing holds survive it)
                                ├─ health check, or roll back to the previous version
                                └─ release its own hold; tell the control channel
```

Step by step:

1. **Resolves the version** — the latest GitHub Release, as the installed Lantern reads it
   (`python -m lantern.releases latest`), or the `workflow_dispatch` input — and
   **short-circuits** if the host already runs it, so the schedule costs nothing when there
   is nothing to do. It then **fetches both releases' wheels** — the target and the version
   installed now, for a rollback — with `python -m lantern.releases download`, which checks
   each against its release manifest. A version it cannot fetch stops the job before any
   hold is taken.
2. **Takes a hold and waits for idle**, polling `ctl status --json` every 15 s with no cap
   short of the job's `timeout-minutes`. A timeout installs nothing: the hold is released and
   the daemon runs on as it was. A daemon that answers nothing for five minutes straight has
   nothing to drain and the job proceeds. To make a deploy go now, `ctl cancel` the run (it
   stays resumable; `cancel --retry` re-queues it fresh).
3. **Snapshots** (`lantern backup`) and **upgrades** from the two fetched wheel files, then
   re-runs `lantern init --systemd --no-sbx` so the launchers and units
   match while preserving the installed sandbox runtime. Rollback also preserves sbx.
4. **Restarts** after `systemctl --user reset-failed`. Holds are persisted, so an operator
   who paused before the deploy or *during* its wait is still paused afterwards without
   the pipeline doing anything.
5. **Health-checks**: unit active, `--version` matches, `lantern doctor` exits 0, the daemon
   answers `ctl status --json`, then a 45 s settle to prove it is not crash-looping.
6. **Rolls back** to the previously installed version's fetched wheels on any failed check,
   restarts, and
   fails the job. Rollback only runs once the upgrade step has — a failure before that
   changed nothing on the host, and a rollback restart would be the needless restart this
   whole procedure exists to avoid.
7. **Releases its own hold** on `always()` — it survives the restart, so a job that died
   after restarting would otherwise leave the daemon paused — and **reports** with
   `lantern daemon notify`, including how long it waited and whether a failure happened
   before anything was installed.

## Upgrading the sandbox runtime

A Lantern deployment or rollback leaves sbx unchanged. Upgrade that runtime separately
with an explicit version, using `lantern init --sbx-version X.Y.Z` after checking
compatibility on a CI runner. Take a named hold and drain the current run first, then stop
`lantern-daemon` followed by `sbx-sandboxd` before installing. Restart the sandbox backend
before the Lantern daemon, check health, and release the hold you took. On a systemd host,
add `--systemd` to that init so the `sbx-sandboxd` unit is rendered afresh: an older unit
runs sandboxd as `Type=simple`, and sbx 0.45's `daemon start`, which detaches on its own,
crash-loops under it.

Before the first start of the new runtime, keep a matching backup of the old binaries and
the stopped sandbox state, configuration and credentials. `lantern backup` does not include
sbx's state or binaries. A runtime downgrade can fail after a newer version migrates its
database, so restoring only the old binaries is insufficient; see
[Docker's downgrade guidance](https://docs.docker.com/ai/sandboxes/troubleshooting/#daemon-fails-to-start-after-downgrading).

## What rollback means for the schema

Step 6 is the reason the database migrations are **additive only**.

A rollback reinstalls the previous version and restarts it against the database the new
one already migrated. It does **not** restore the snapshot step 3 took — that snapshot is
there for an operator to reach for deliberately, not for the job to unwind with. So the
version being rolled back to has to keep reading the file correctly.

In practice that means a revision may add a table, or add a column that is nullable or
carries a default. It may not rename, drop or retype anything the previous version reads.
Where a column replaces an older one, the release that adds it keeps writing both, and
only a later release — once no deployed version depends on the old shape — stops.
`reconciliations.kind` is the worked example: it names what a row is about, while `round`
goes on carrying the sentinel the previous version reads.

The store applies migrations when it opens the database, so there is no step to add here
and nothing to run by hand. `tests/unit/test_db_schema.py` holds the check: a database
written by a released version is migrated, read back, written to, and then handed to the
*previous* version's migrator, which must find nothing to do.

Two settings, and no names in the file:

- The repository variable **`LANTERN_DEPLOY_HOST`** is the runner label the job targets
  (`runs-on: [self-hosted, "${{ vars.LANTERN_DEPLOY_HOST }}"]`) and the name the notices
  call the host. Moving the daemon to another host is registering a runner there with that
  label — or changing the variable; the workflow file does not change.
- The **`schedule`** is how often the host checks for a new release.

### Where the job deploys to

The job's first step resolves the Lantern home **once** and derives every path it touches
from that root — the launcher, the venv interpreter, the home's `uv` and its caches, and
through `LANTERN_HOME` in the job environment, the backup, `init`, `doctor`, the health
check and the rollback too. Nothing downstream re-derives a root, so a deploy cannot
address one installation and health-check another. It resolves in this order:

1. The repository variable **`LANTERN_HOME`**, if set.
2. The runner process's own `LANTERN_HOME`.
3. `${HOME}/.lantern` — the default `lantern init` builds.

The resolution happens in a step rather than in a job-level `env:` because those values are
literals: GitHub does not expand `${HOME}` there. A root that is not absolute, or that
holds a newline, fails the job before anything on the host is touched — there is no working
directory to make a relative path mean anything, and every step would read it differently.
A `~/`-prefixed root expands against the service user's home, the same as the loader does.
A home whose path contains spaces is carried through as itself.

So a host `lantern init` built with the default home needs no edits. A host installed under
a custom `LANTERN_HOME` needs the job to be told, and there are two ways:

- **The runner unit.** `lantern init --systemd --runner DIR` renders
  `github-runner.service` with `Environment=LANTERN_HOME=<the home it just built>`, so a
  runner started from that unit already hands every job the right root. This is the path to
  prefer: the home comes from the installation itself and cannot fall out of step with it.
- **The repository variable.** Where the runner was registered some other way — GitHub's
  `svc.sh`, a container, a shell — set the repository variable `LANTERN_HOME` to the same
  absolute path. It wins over the runner's environment, which is what makes it useful for
  correcting a runner that carries the wrong one. Leave it unset on a default install.

### `lantern daemon notify`

Posts one message through the configured `[chat] backend`, from the host and without the
daemon — so a script can say "rollback also failed" while the daemon is down. By default it
reads the control channel from the home's `lantern.toml`; `--channel <id>` can route a
purpose-specific notice elsewhere through the same backend. It reads the bot token from the
environment (`DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN` or `MATTERMOST_BOT_TOKEN`, from the home's
`secrets.env`), so the workflow never sources a secrets file or parses the config itself.
The text is the chat's Markdown; on Slack it is re-dialected the way the bridge does it.
Link previews and pings are suppressed. A headless daemon (no chat backend) cannot notify,
and says so.

### The runner

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -fsSLO https://github.com/actions/runner/releases/download/v<X>/actions-runner-linux-x64-<X>.tar.gz
echo "<sha256>  actions-runner-linux-x64-<X>.tar.gz" | sha256sum -c -
tar xzf actions-runner-linux-x64-<X>.tar.gz

# registration tokens expire in an hour; mint one at use time
./config.sh --url https://github.com/<owner>/<repo> \
  --token "$(gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token --jq .token)" \
  --name <host> --labels <host> --work _work --unattended --replace

# render the unit for this runner directory, from anywhere — init enables it
lantern init --systemd --no-sbx --runner "$HOME/actions-runner"
systemctl --user start github-runner
```

`self-hosted`, `Linux` and `X64` are added automatically; `--labels <host>` is the one
`LANTERN_DEPLOY_HOST` must match.

The unit is not a file to copy: the packaged template
([contrib/systemd/github-runner.service](../contrib/systemd/github-runner.service)) carries
an `@RUNNER@` placeholder where the runner directory goes, and `init --systemd --runner DIR`
is what puts the absolute path in it, writes it into `~/.lantern/systemd/`, enables it from
there and turns lingering on. So the command needs no source checkout — a wheel or
`install.sh` installation has everything — and runs from any directory, including the runner
directory itself. `--no-sbx` leaves an already-installed sandbox runtime alone; init only
*enables* units, never starts them, which is why `start` is a separate line. Re-running init
later without `--runner` leaves this unit where it is but stops refreshing it, so carry the
flag on the host's init line — including the one in the upgrade sequence in
[contrib/systemd/README.md](../contrib/systemd/README.md).

The runner is a *user* unit rather than GitHub's `svc.sh` system unit: a system service has
no `XDG_RUNTIME_DIR` or `DBUS_SESSION_BUS_ADDRESS`, so
`systemctl --user restart lantern-daemon` fails there with
"Failed to connect to bus". `KillMode=process` so stopping the runner never cuts off a
deploy mid-restart, and `Environment=LANTERN_HOME=` carries the home init built so every
job on the runner deploys to this installation. Confirm with
`gh api repos/<owner>/<repo>/actions/runners --jq '.runners[].status'`.

### Security

A self-hosted runner executes whatever a workflow says, as its user — who can restart
services on the host and, on many hosts, has `sudo`. So:

> **No job carrying the `self-hosted` label may be reachable from a fork-triggerable event** —
> `pull_request`, `pull_request_target`, or `issue_comment`.

A fork-triggered job on that runner would be a full host compromise. The example uses only
`schedule` (which runs the default branch in base-repository context) and
`workflow_dispatch` (which needs write access). Keep the invariant as a comment at the top
of the workflow so it survives future edits.

Supporting controls: protect the default branch with required status checks and
`enforce_admins`; leave default workflow permissions at `read` (the job narrows to
`contents: read`); and set *Fork pull request workflows from outside collaborators* to
**require approval for all external contributors**. Runner groups are org-only, so
per-workflow runner scoping is not available on a personal repository — the label invariant
is the control.

## Relationship to `version_status`

The concierge's `version_status` tool and the startup drift line report when a host is
behind the latest GitHub Release; the upgrade paths above are what stops it happening. The concierge deliberately
cannot upgrade anything. On a host upgraded by hand, set `[daemon] upgrade_command` to the
two-command path (or whatever wraps it) so the notice tells the operator exactly what to run.
On a host *with* the workflow, set `[daemon] version_check = false`: the workflow is what
keeps the host current, a hand upgrade in between would be rolled to wherever the next
scheduled deploy lands, and the notice would only ever advise exactly that — so the host
makes no release request and gives no advice, and a stale host shows up in the workflow's run
history instead.

## Multiple repositories on one host

One daemon tends every repository declared in `lantern.toml`, so a second
project does **not** need a second unit, home or control channel.
Declare them as `[[vcs.repos]]` entries, whatever forge they live on — each
with its own `deliver_base`, `trigger_label`, extra `labels`, `enabled`
switch and optional `token_env` — and export any per-repo token from the
home's `secrets.env` alongside `GH_TOKEN`. The legacy spellings, `[[github.repos]]`
and the single `[github] repo = "owner/name"`, still load unchanged and are
folded into the same list; `lantern config migrate` rewrites either in place
with every comment kept and the previous file backed up. A file that declares
repositories under both spellings, a duplicated repository or a malformed
slug fails config loading. Work items
queued by the pre-migration single-repo daemon carry no repository. At startup
the daemon attributes what it can from each row's issue URL; of the rest, only
items still sitting untouched in the queue are discarded and rediscovered,
repo-qualified, on the next poll — an issue still carrying `lantern:run` is
simply picked up again. An item that was already **claimed** (or running) is
not: claiming replaces `lantern:run` with `lantern:in-progress`, so discovery
will never see that issue again. Those items are failed with an explicit
reason instead of being dropped, and the daemon logs
`daemon.repoless_items_stranded` (and posts a control-channel notice) naming
each item id and issue URL, so you can clear the in-progress label and re-add
`lantern:run` by hand for anything that was in flight across the upgrade.

Everything under `[[vcs.repos]]` is per repository; the `[daemon]`
guardrails — the daily run cap, the per-item attempt and resume caps, the
consecutive-failure circuit breaker, and the concurrency cap — stay
**daemon-wide** and are shared across all of them. Polling health is the
one per-repository guardrail (#516): a repository that fails to poll is
backed off on its own (doubling, capped at an hour) and, after
`[daemon] repo_suspend_after` consecutive failures — or at once when GitHub
says it is gone for this token (404/410, a permission 403) — **suspended**
from polling, announced once in the control channel, shown in `ctl status`, the
concierge's repository listing and `lantern doctor`, and resumed with
`lantern daemon ctl resume-repo <owner/name>` (or a daemon restart, which
starts every repository fresh). The healthy repositories poll on as usual. That is what makes one
unit the right shape: the host's budget is bounded in total, and a
repository that keeps failing trips the breaker for the whole daemon. Deploy
health checks are unaffected; `lantern doctor` reports one row per
configured repository, so a broken repo is visible without masking the rest.

## When it all goes wrong

If a deploy fails *and* its rollback fails, the job says `ROLLBACK ALSO FAILED — <host> needs a human`. Fix by hand: the two commands under [Upgrading by hand](#upgrading-by-hand)
with the last good version, then `journalctl --user -u lantern-daemon -n 200` for why the
new one would not start.

## From the console

`lantern tui` on the host does the same from its Daemon screen: the unit's
state, start / stop / restart (typed), the journal streamed with a grep and
a level floor, versions and the upgrade command, and a graceful `stop`
through the daemon's own `ctl` queue. See [tui.md](tui.md#daemon-6).
