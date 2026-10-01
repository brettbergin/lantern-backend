# How Lantern deploys itself

**This is a reference for this repository's own daemon host, not a guide.** The generic
procedure — running the daemon as a service, upgrading it by hand, automating that with a
workflow — is [docs/deploy.md](deploy.md). This page records where lantern's own pipeline
departs from that pattern and the facts about the host that operating it needs.

```
merge to main → coalesced request → Release (verify + tag + GitHub Release) → Deploy the daemon
                                                 ├─ check version, cooldown, and blocked releases
                                                 ├─ check the existing host with doctor
                                                 ├─ take a named pause hold (deploy-<run id>)
                                                 ├─ wait — no cap — for the in-flight run to finish
                                                 ├─ refresh to the latest complete release
                                                 ├─ pip install the release wheels
                                                 ├─ systemctl --user restart lantern-daemon
                                                 ├─ health check, or roll back
                                                 └─ restore the other holds + tell the control channel
```

Merges share a release after three quiet minutes, with a maximum thirty-minute batch
wait ([RELEASING.md](../RELEASING.md)). `.github/workflows/deploy.yml` carries completed
releases onto the daemon host. A task may already be in flight when a release arrives,
which is why draining has no cap apart from the job timeout (#534): restarting anyway
killed tasks and spent their resume budgets.

Release reuses successful CI for its frozen commit, or runs the same CI workflow
when that evidence is unavailable. Its automatic intake coalesces pending requests;
manual release requests retain their place in the publication queue. The final
wheels pass a clean installation and CLI smoke test before publication. Deployment
still starts from the completed `Release` workflow and its publication receipt.

Automatic deployments have a thirty-minute cooldown after a deployment finishes,
including a verified rollback. No hold is taken during that cooldown. A reconciliation
schedule at minutes 7, 22, 37 and 52 retries deferred releases even if no further merge
arrives. It creates no tags or packages and does nothing when the host is current.
GitHub can delay scheduled jobs; this is eventual reconciliation, not a deadline.

At idle, automatic and manual "latest" deployments select the newest complete stable
release again. If B and C published while A waited, the host installs C directly.
Explicit manual versions remain pinned. Automatic deployment never downgrades a host.
The selected version is frozen before downloading, backing up, installing, and checking
health; reports and history use that final version.

## Where it departs from the example

`deploy.yml` is `contrib/workflows/deploy-daemon.yml.example` with these differences:

- **Trigger.** Successful `workflow_run` on `Release`, reconciliation `schedule`, and
  `workflow_dispatch` restricted to `main`. The completion's `release-result` artifact
  explicitly distinguishes a publication from a no-op. A missing/malformed artifact
  fails the event-triggered attempt; reconciliation can still find a completed release.
- **Version.** The greatest stable numeric version with a published GitHub Release and
  both wheels and source distributions uploaded. Drafts are ignored; an incomplete
  published release fails closed. The tag must resolve to a commit on `main`.
- **Workflow helper.** No checkout runs on the host. The job downloads
  `scripts/release_pipeline.py` at its own `github.workflow_sha`, with the workflow's
  token. Only this trusted workflow revision supplies executable helper code. The
  release result and manifest are parsed as data. The token needs `contents: read`
  and `actions: read` to read releases and the completion artifact.
- **Wheels from the release, never an index.** Releases are GitHub Releases only (v2.1.36
  was the last on PyPI), and the names a rename moves to are not ours on any index, so
  neither package is ever installed by name. The helper downloads the release's exact
  wheel files, checking each asset's size and SHA-256 digest, and `verify-download` checks
  them against the manifest. The host wheel pins `lantern-worker==X` exactly, and naming
  the local worker wheel satisfies that pin without the index being consulted. Rollback
  fetches the previous version's wheels from its own release the same way. Both install
  `[discord,slack]` (#619) so a rollback never drops an extra the upgrade had.
- **Manifest.** New releases carry `release-manifest.json`; the downloaded wheels must
  match its hashes and its commit must match the release tag. Existing completed releases
  without that manifest remain deployable for rollback compatibility.
- **Deployment state.** `state/deploy.json` under the resolved Lantern home records the
  last completion time, versions suppressed after attempts/rollbacks, and whether an
  upgrade was interrupted. Writes replace the file atomically. Malformed state and an
  interrupted or unhealthy deployment block automatic work and name the recovery need.
- **`GH_TOKEN` is set explicitly** on every step calling `gh`. The host's `secrets.env`
  exports its own `GH_TOKEN`, and the `lantern` wrapper sources it with `set -a`; without
  the override, a host PAT would be the identity for Actions API calls.
- **The home.** Everything is under `~/.lantern`, exactly the layout the generic guide
  describes; the tokens live in `~/.lantern/config/secrets.env` (mode 0600; shape: the
  repo-root [`.env.example`](../.env.example)), read by Lantern itself. The job never
  reads that file (#639): `ctl status --json`, `backup` and `daemon notify` go through the
  launcher, from whatever directory the runner happens to be in. Deploy notices land in
  `#lantern-deploys`; the trusted workflow helper selects that channel through the installed
  notifier's original API, so routing also works before an upgrade or after a rollback. The
  daemon's normal control traffic remains in `#lantern`.
- **PDF analyzer identity.** Before a changed release takes the deploy hold, the job
  signs in to Docker under the separate `lantern-analysis` app with repository
  Docker Hub secrets and initializes a deny-all policy once. The PDF worker runs with no
  agent skills, credentials or network access. A missing Docker Hub secret fails
  the deployment before the installed daemon is changed.

## The host

`db`, one user, three user units alongside each other: `lantern-daemon`, `sbx-sandboxd`
(the sandbox backend, which the daemon unit `Requires=`), and `github-runner` (the Actions
runner, as the same user so the workflow can `systemctl --user restart`). Deploying by hand
and the layout table are in the generic guide; the paths above are the only deltas.

The runner is registered with the label `db`:

```bash
./config.sh --url https://github.com/brettbergin/lantern-backend \
  --token "$(gh api -X POST repos/brettbergin/lantern-backend/actions/runners/registration-token --jq .token)" \
  --name db --labels db --work _work --unattended --replace
```

Confirm with `gh api repos/brettbergin/lantern-backend/actions/runners --jq '.runners[].status'`.

**The host is one variable** (#640). The workflow targets
`runs-on: [self-hosted, "${{ vars.LANTERN_DEPLOY_HOST || 'db' }}"]` and calls the host by
the same name in its notices. To move the daemon: set the repository variable
`LANTERN_DEPLOY_HOST` to the new host's runner label and register a runner there with that
label. `deploy.yml` does not change, and neither does anything `make check` runs.

## Operating it

```bash
# deploy a specific version (also the rollback path)
gh workflow run deploy.yml --ref main -f version=X.Y.Z

# deploy whatever the latest release is
gh workflow run deploy.yml --ref main

gh run watch                                            # from the repo
ssh db 'journalctl --user -u lantern-daemon -f'         # from the host
ssh db 'systemctl --user status github-runner sbx-sandboxd lantern-daemon'
```

Rolling back is just deploying the older version — the workflow pins exactly. A rollback
passes only after its version, service, doctor and control checks pass. If a deploy
fails *and* its rollback fails, the job says `ROLLBACK ALSO FAILED — db needs a human`; fix
by hand with the commands in the generic guide, from any directory. Every deploy leaves a
snapshot under `~/.lantern/backups/` and a line in `~/.lantern/logs/deploy/history.log`.

Manual deploys bypass the cooldown and failed-version suppression, but wait for the
shared deployment slot and for the current daemon task to drain. Pending manual requests
are retained (`queue: max`, at most 100 pending runs). An explicit manual recovery can
reinstall the same version after an interrupted deployment; it still must pass preflight.

A failed upgrade followed by rollback suppresses that target and older versions from
automatic retries. A manual rollback suppresses automatic releases through the latest
published version observed when that rollback starts, so the next schedule cannot undo
it. A newer version or an explicit manual retry can proceed. If rollback cannot restore
health, repair the host manually; after repair, use a manual deployment to verify health
and clear the interrupted state. To stay pinned across future releases, disable the
workflow and use the by-hand procedure until automatic deployment is wanted again.

Cooldown/current/blocked decisions appear in the Actions summary without chat notices.
The control channel sees actual draining, a changed target after draining, and the final
deployment or rollback outcome. Normal successful upgrades are spaced apart; manual
operations and necessary rollback recovery can restart sooner.

Set `[daemon] version_check = false` in this host's `lantern.toml` (#641): the pipeline keeps it current,
so the daemon neither asks GitHub Releases nor advises a hand upgrade the next deploy would undo. A
stale host shows up as a failed or skipped **Deploy the daemon** run — check that, not the
concierge.

## Cutovers

Because this pipeline deploys unattended, a change to what the deploy job needs from the
running daemon can strand it. Each one is recorded here with its manual step.

- **Structured status (#639).** The drain step now reads `ctl status --json` and
  fails closed when the daemon answers without a structured status — which a daemon older
  than that flag does. The first deploy after it lands therefore fails at "Wait for the
  daemon to go idle" *before installing anything*; upgrade once by hand (the two commands in
  the generic guide), and every deploy after that is unattended again.
- **1.0 (state and config).** The steps a 0.7.x host needs are in the
  [CHANGELOG under "1.0 cutover"](../CHANGELOG.md#10-cutover).
- **The home.** Every path moved under `~/.lantern` and the deploy job's paths with them,
  so the first deploy after it lands fails at "Resolve host paths"' consumers (no
  `~/.lantern/bin/lantern` yet) *before installing anything*. The one manual step, once,
  as the service user: install the release into a fresh home and migrate the old
  installation into it —
  `LANTERN_VERSION=X.Y.Z LANTERN_INIT_ARGS="--migrate --purge --runner $HOME/actions-runner" sh <(curl -fsSL …/scripts/install.sh)`
  — then `lantern doctor` and re-run the deploy. The migration backs up the old config,
  secrets, units and every `state.db` it finds to `~/.lantern/backups/<stamp>-migrate/`
  first.
- **sbxloop became Lantern.** The package, CLI, environment prefix, config file, unit
  and home were all renamed (`lantern`, `LANTERN_*`, `lantern.toml`, `lantern-daemon`,
  `~/.lantern`) with no aliases, so "Deploy the daemon" stops at "Resolve host paths" on a
  host that still has `~/.sbxloop`. Run **Cut over from sbxloop** once
  (`gh workflow run cutover-from-sbxloop.yml --ref main -f version=X.Y.Z`): it drains and
  stops `sbxloop-daemon`, installs the release into a fresh home with
  `lantern init --from-sbxloop ~/.sbxloop` (config, secrets, App key and workspaces carried
  and renamed; state starts fresh), starts `lantern-daemon` and health-checks it, and puts
  sbxloop back if anything fails. `~/.sbxloop` is left untouched as the rollback until
  `-f purge=true` removes it. Configuration management that writes into the home (Ansible
  roles for OIDC or push) must target `~/.lantern/config/lantern.toml` and
  `LANTERN_OIDC_CLIENT_SECRET` from then on.
