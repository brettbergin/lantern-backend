# Running `lantern daemon` under systemd (user service)

Everything lives under the **Lantern home**, `~/.lantern` (`LANTERN_HOME`
moves it): the interpreter, the launchers, the config and secrets, the
state, the runs, the workspaces, the logs, the unit files. One command
builds it.

1. Install and initialise the home:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/brettbergin/lantern-backend/main/scripts/install.sh | sh
   ```

   The host needs curl, tar and git already installed — the script checks
   all three before it downloads anything, git included: Lantern cannot
   start without one. That puts `uv`, a CPython and the
   `lantern-backend[discord,slack]` venv under the home — Lantern and its worker
   from the latest GitHub Release's wheels, checked against its SHA-256
   manifest (`LANTERN_VERSION=X.Y.Z` pins one) — then runs
   `lantern init --systemd`, which writes the launchers
   (`~/.lantern/bin/lantern`, `~/.lantern/bin/sbx`), installs Docker's
   `sbx` under `~/.lantern/sbx`, writes `config/lantern.toml` and a 0600
   `config/secrets.env`, renders the units into `~/.lantern/systemd`
   and enables them (never starts them), and turns lingering on so user
   units outlive the login. Put `~/.lantern/bin` on your `PATH`. Already
   have Lantern installed some other way? `lantern init --systemd` from
   that install builds the same home; `lantern init --migrate --purge`
   moves a pre-home installation into it first.

2. Fill in the config and the secrets, then check:

   ```bash
   $EDITOR ~/.lantern/config/lantern.toml    # [github], [daemon], [discord] / [slack]
   $EDITOR ~/.lantern/config/secrets.env     # the tokens; lantern reads this file itself
   sbx login && sbx policy init balanced     # the home's sbx, through its wrapper
   lantern doctor                            # tokens, sbx, the home, the units
   ```

   One daemon can tend several repositories: declare them as
   `[[github.repos]]` entries. Each entry carries its own base branch,
   labels, `enabled` switch and optional `token_env` — export any per-repo
   token in `secrets.env`. The `[daemon]` guardrails (daily run cap,
   attempt/resume caps, circuit breaker, one run at a time) are daemon-wide
   and shared across all of them, so one service is enough.

   The daemon keeps a **dedicated clone** of each repository under
   `~/.lantern/workspaces/<owner>/<name>`, cloned on first use and
   fast-forwarded before every run; nothing to set up. Point
   `[sandbox] workspace` (or a repo entry's `workspace`) at a checkout of
   your own only if you want that one used instead — never the checkout
   you work in. Run state (per-run clones, artifacts, SQLite) lands under
   `~/.lantern/runs` and `~/.lantern/state`; the daemon logs its home in
   its `daemon.starting` line.

3. Start it and watch it:

   ```bash
   systemctl --user start lantern-daemon     # sbx-sandboxd starts first (Requires=)
   systemctl --user status lantern-daemon
   journalctl --user -u lantern-daemon -f    # or: lantern daemon logs -f
   # only the runs' own lifecycle (tasks, phases, sandboxes, worker jobs):
   journalctl --user -u lantern-daemon -f | grep lantern.run
   ```

   The log is structured (`event key=value …`) and also written to
   `~/.lantern/logs/daemon.log` (rotated by size). `[daemon] log_level = "DEBUG"` in `config/lantern.toml` turns on the per-call firehose, and
   `log_format = "json"` renders one JSON object per line for a log
   shipper.

`systemctl --user stop` sends SIGTERM: the daemon stops claiming work, asks
the in-flight run to cancel at its next task boundary, waits up to
`[daemon].shutdown_grace_s`, and exits. The interrupted run stays resumable
and is picked up on the next start.

## Upgrading

By hand, as the daemon's user, once nothing is running. Take a named hold
so the daemon stops claiming, wait for idle, snapshot, install the exact
version into the home's venv — `init --version` fetches that GitHub
Release's wheels and checks them against its manifest; Lantern is never
installed by name from a package index — re-run init (idempotent: it
refreshes the launchers and units for the new version and keeps your
config), restart. Use `--no-sbx` to preserve the installed sandbox runtime:

```bash
lantern daemon ctl pause --hold upgrade
until [ "$({ lantern daemon ctl status --json 2>/dev/null || echo '{}'; } | jq -r '.current // .claiming // "idle"')" = idle ]; do sleep 15; done

lantern backup --label pre-X.Y.Z
lantern init --no-sbx --version X.Y.Z
lantern init --systemd --no-sbx
systemctl --user reset-failed lantern-daemon && systemctl --user restart lantern-daemon
```

Every command runs from any directory: the home is the home. On a host
installed under a custom `LANTERN_HOME`, read `~/.lantern` above as that
root — `lantern` reads the variable itself. `lantern update` is the same
install for the newest release. `reset-failed` matters: `StartLimitBurst=5` per
600s leaves a unit that crash-looped in `failed`, where a plain `restart`
will not revive it. The daemon comes back **unpaused** regardless — holds are in-memory only — so re-take any you want
to keep. A downgrade is the same commands with an older version, or
`lantern backup restore <name>` for the config and state of a snapshot.

These commands upgrade or roll back Lantern only. Change sbx separately with an explicit
`lantern init --sbx-version X.Y.Z` after compatibility checks, draining the daemon and
backing up the stopped runtime's state and binaries; see
[Upgrading the sandbox runtime](../../docs/deploy.md#upgrading-the-sandbox-runtime).

To automate exactly this (plus a health check and rollback) from a GitHub
Actions runner on the host, copy
[contrib/workflows/deploy-daemon.yml.example](../workflows/deploy-daemon.yml.example)
into the repository that owns the host; [docs/deploy.md](../../docs/deploy.md)
walks through it.

## The units

The files in this directory are the templates `lantern init --systemd`
renders with the home's absolute paths into `~/.lantern/systemd/` and
enables from there (`systemctl --user enable <path>` links them into
`~/.config/systemd/user`). Do not copy them by hand: a re-run of init
rewrites the rendered copies.

`sbx-sandboxd.service` supervises the sandbox backend through the home's
`sbx` wrapper. Without it `sbx daemon start` is a bare process: if it dies
nothing restarts it, and every run fails with no systemd trace. The daemon
unit `Requires=` it, so a manual `systemctl --user start lantern-daemon`
brings the backend up first. The unit starts sandboxd detached (`-d`,
`Type=forking`) and follows the pid file it writes under sbx's state directory
(`$XDG_STATE_HOME/sandboxes/sandboxes/sandboxd/sandboxd.pid`, `~/.local/state`
by default), since newer sbx releases detach whether asked to or not.

`github-runner.service` is **only needed for the automated upgrade above**.
It runs a GitHub Actions runner as the *same user*, which is what lets a
workflow do `systemctl --user restart lantern-daemon`;
`lantern init --systemd --runner ~/actions-runner` renders it — with that
directory's absolute path in `WorkingDirectory=` and `ExecStart=`, and
`LANTERN_HOME` set to the home it was rendered against, so a job on that
runner upgrades the installation that is there rather than assuming the
default. That is why the template is never copied by hand. Carry
`--runner DIR` on every later init on that host, the upgrade one above
included, or the unit stops being refreshed. Skip all of it if you upgrade
by hand. All three are user units, so `loginctl enable-linger` (which init
does) covers them.
