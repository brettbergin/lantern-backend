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
wait ([RELEASING.md](../RELEASING.md)). A job on another runner carries each published
release onto the daemon host by running `scripts/deploy_daemon.py`
([Running the deploy from another runner](#running-the-deploy-from-another-runner)).
`.github/workflows/deploy.yml` is the same pipeline, kept as a manual break-glass that
never starts by itself. A task may already be in flight when a release arrives, which is
why draining has no cap apart from the job timeout (#534): restarting anyway killed tasks
and spent their resume budgets.

Release reuses successful CI for its frozen commit, or runs the same CI workflow
when that evidence is unavailable. Its automatic intake coalesces pending requests;
manual release requests retain their place in the publication queue. The final
wheels pass a clean installation and CLI smoke test before publication. Deployment
starts from the published GitHub Release itself: the other runner's job runs when one is
published and compares it with what is installed. The `Release` workflow's publication
receipt is no longer read.

Automatic deployments have a thirty-minute cooldown after a deployment finishes,
including a verified rollback. No hold is taken during that cooldown. Nothing retries on
a schedule any more: the job that meets the cooldown waits it out itself
(`prepare --wait-cooldown`), decides again, and deploys the newest release it finds then,
even if no further merge arrives. A job that finds the host current does nothing. The
old schedule also retried a release whose start had been missed; that is now the other
runner's to cover, and the next published release, or a manual run, deploys the newest
release either way.

At idle, automatic and manual "latest" deployments select the newest complete stable
release again. If B and C published while A waited, the host installs C directly.
Explicit manual versions remain pinned. Automatic deployment never downgrades a host.
The selected version is frozen before downloading, backing up, installing, and checking
health; reports and history use that final version.

## Where it departs from the example

`deploy.yml` is `contrib/workflows/deploy-daemon.yml.example` with these differences:

- **Trigger.** `workflow_dispatch` only, restricted to `main`: the workflow is the manual
  break-glass and never starts by itself. The example's `schedule` has no counterpart
  here, because the automatic start is another runner's (below). The step that read the
  `release-result` artifact of a completed `Release` is still in the file, but a dispatch
  never takes that branch.
- **Version.** The greatest stable numeric version with a published GitHub Release and
  both wheels and source distributions uploaded. Drafts are ignored; an incomplete
  published release fails closed. The tag must resolve to a commit on `main`.
- **Workflow helper.** No checkout runs on the host. The job downloads
  `scripts/release_pipeline.py` at its own `github.workflow_sha`, with the workflow's
  token. Only this trusted workflow revision supplies executable helper code. The
  manifest is parsed as data. The token has `contents: read` to read releases;
  `actions: read` was for the completion artifact, which a dispatch does not read.
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
label. `deploy.yml` does not change, and neither does anything `make check` runs. The
other runner's deploy job names the host in its own settings (`HOST`, and the home), so
those move with the daemon too.

## Operating it

Day to day nothing is operated here: the other runner deploys each published release. To
deploy a chosen version, or to roll back, start a manual run of its deploy job with the
version (`INPUT_VERSION`); the same manual rules apply as to a dispatch here.

`deploy.yml` is the break-glass, for when that runner cannot deploy. Its concurrency
group serializes runs of the workflow only and knows nothing of the other runner. So
before dispatching by hand, make sure the other runner's deploy is not running and cannot
start (pause it there), and resume it afterwards: the two must never run together. A hold
is released by the side that took it. The workflow releases only its own
`deploy-<run id>`; a hold the other runner's job left standing is released there, or by
hand ([What it does not cover](#what-it-does-not-cover)).

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
current daemon task to drain. Dispatches of `deploy.yml` also wait for the workflow's own
deployment slot, and pending ones are retained (`queue: max`, at most 100 pending runs);
that slot does not include the other runner. An explicit manual recovery can
reinstall the same version after an interrupted deployment; it still must pass preflight.

A failed upgrade followed by rollback suppresses that target and older versions from
automatic retries. A manual rollback suppresses automatic releases through the latest
published version observed when that rollback starts, so the next automatic deploy cannot
undo it. A newer version or an explicit manual retry can proceed. If rollback cannot
restore health, repair the host manually; after repair, use a manual deployment to verify
health and clear the interrupted state. To stay pinned across future releases, pause the
other runner's deploy job and use the by-hand procedure until automatic deployment is
wanted again.

Cooldown/current/blocked decisions appear in the job's step summary, without chat notices.
The control channel sees actual draining, a changed target after draining, and the final
deployment or rollback outcome. Normal successful upgrades are spaced apart; manual
operations and necessary rollback recovery can restart sooner.

Set `[daemon] version_check = false` in this host's `lantern.toml` (#641): the pipeline keeps it current,
so the daemon neither asks GitHub Releases nor advises a hand upgrade the next deploy would undo. A
stale host shows up as a failed deploy job on the other runner: check that, not the
concierge.

## Running the deploy from another runner

`deploy.yml` needs GitHub Actions: its steps carry `if:` conditions, `always()` and step
outputs. `scripts/deploy_daemon.py` is the same pipeline for a runner that has none of
them: one that runs a list of steps in order, stops at the first failure, and then runs a
second list whatever happened. The cutover has happened: a job on another runner, started
by each published release, is the deploy now, and `deploy.yml` stays as the manual
break-glass. The two must never run together ([Operating it](#operating-it)).

The script has three entry points, called as three steps of one job: two in order, and
one that always runs.

```bash
python3 scripts/deploy_daemon.py prepare --wait-cooldown
python3 scripts/deploy_daemon.py deploy
# always, whatever the two above did; the status is the job's:
# success, failure, timed_out or cancelled
python3 scripts/deploy_daemon.py finish --status "$STATUS"
```

| Entry point | The steps of `deploy.yml` it performs                                                                                             | Exit code                                                                                                                  |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `prepare`   | "Resolve host paths" through "Wait for the daemon to go idle"                                                                     | 0 with the daemon held and idle, or with the host untouched when there is nothing to deploy; non-zero on any failure       |
| `deploy`    | "Refresh the target after draining" through "Refresh the baked template", and "Roll back" when anything from the upgrade on fails | non-zero when the deploy failed, whether or not the rollback restored the previous version; 0 when there was nothing to do |
| `finish`    | the `always()` steps: record the outcome in `state/deploy.json`, release the hold, remove the downloads, report                   | 0, unless the outcome could not be recorded or the job's own state could not be trusted                                    |

Each step does what its workflow step does, under the same condition and in the same
order, and the notices are the same texts. `finish` needs nothing from the steps before
it. When `prepare` never ran it does nothing; after a step that was cut short it releases
the hold only if this job had asked for one, and records an outcome only if the upgrade
had started. A job that is cancelled or runs out of time is not rolled back, as under
Actions: if the upgrade had started, `state/deploy.json` keeps `in_progress` and
automatic deploys stop until a manual one clears it.

The three steps share one directory, `deploy-daemon-<run id>` under `RUNNER_TEMP` (mode
0700, removed by `finish`). It holds what the workflow kept in `steps.*`, and the
downloaded wheels. `GITHUB_ENV` and `GITHUB_OUTPUT` are neither read nor written. There is
no `github.workflow_sha` to fetch the helper at: the runner supplies
`scripts/deploy_daemon.py` and `scripts/release_pipeline.py` side by side from a trusted
revision of `main`, never from a pull request head or a release asset. The script is
stdlib-only and runs under Python 3.10 or newer; the helper still runs under the home's
own interpreter.

### What the job's environment carries

What `deploy.yml` took from the `github` context, repository variables and secrets
arrives as environment variables, under the names its `env:` blocks use.

| Variable                                | Meaning                                                                                                                                           |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GITHUB_REPOSITORY`                     | Required. The repository whose releases deploy.                                                                                                   |
| `GITHUB_RUN_ID`                         | Required. Names the hold, the job's directory and the `run=` of the history line.                                                                 |
| `GITHUB_EVENT_NAME`                     | `workflow_dispatch` is a manual run; anything else (`release`, `schedule`) is automatic.                                                          |
| `INPUT_VERSION`                         | The version to deploy; empty for the latest complete release.                                                                                     |
| `RUNNER_TEMP`                           | Required. The job's temporary directory, an absolute path.                                                                                        |
| `GITHUB_STEP_SUMMARY`                   | Optional. The helper appends its decision line to it.                                                                                             |
| `LANTERN_HOME_VAR`, `LANTERN_HOME`      | The home, resolved as the workflow's first step does: `LANTERN_HOME_VAR` (the repository variable there), else `LANTERN_HOME`, else `~/.lantern`. |
| `HOST`                                  | Required. The host, as the notices name it.                                                                                                       |
| `DEPLOY_CHANNEL`                        | Required. The channel the deploy notices go to.                                                                                                   |
| `UNIT`                                  | The user unit to restart; `lantern-daemon` when unset.                                                                                            |
| `RUN_URL`                               | The link the notices give for the run. When unset it is composed as Actions does, from `GITHUB_SERVER_URL`; one of the two is required.           |
| `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` | The PDF analyzer's sign-in. Needed once there is a release to deploy, and handed to the analyzer's `sbx` commands only.                           |
| `DEPLOY_GITHUB_TOKEN`                   | The job's GitHub token (`contents: read`). `--token-env NAME` or `DEPLOY_TOKEN_ENV` names another variable; `GH_TOKEN` is the fallback.           |
| `DEPLOY_HOLD_PREFIX`                    | Optional, as is `prepare --hold-prefix`: a prefix for the hold's name.                                                                            |
| `DEPLOY_JOB_STATUS`                     | The job's status for `finish`, when `--status` is not given.                                                                                      |

### Where it departs from `deploy.yml`

- **No receipt.** There is no `workflow_run` event and no `release-result` artifact, so
  `prepare` goes straight to the version check, as a dispatch of `deploy.yml` does.
- **Cooldown.** `deploy.yml` used to run every fifteen minutes to pick up a release the
  cooldown had deferred. That schedule is gone, so the job waits instead:
  `prepare --wait-cooldown` sleeps until the cooldown has passed, decides
  again, and goes on with what it finds then: a newer release, or nothing to do. It waits
  before doctor and before any hold. A manual run never waits, because the cooldown does
  not apply to it. Without the flag a cooldown ends the job successfully, as it always did.
- **The token never reaches lantern.** Actions scoped its token to the steps that call
  GitHub; a runner that puts one token in every step's environment cannot. The script
  passes the token as `GH_TOKEN` to the release helper's GitHub subcommands and to nothing
  else, and removes `GH_TOKEN`, `GITHUB_TOKEN`, `DEPLOY_GITHUB_TOKEN` and any variable
  named in its place from the environment of every `lantern`, `systemctl`, `uv` and `sbx`
  command it runs, so those keep the host's own credentials. A runner that keeps its token
  under a name of its own sets `DEPLOY_TOKEN_ENV` to that name, or passes `--token-env`.
  The notifier and `deploy-finish` get no token either, as in the workflow. One call is
  the helper's own, not the script's: `deploy-check` runs `lantern --version` itself, and
  that call sees the helper's `GH_TOKEN`, as it does under Actions.
- **The hold's name** is `deploy-<run id>`, with the prefix in front when one is set, so
  two runners whose run ids overlap cannot take or release each other's hold. `finish`
  releases the name `prepare` recorded.
- **The job's status** is the runner's word. `success`, `failure` and `cancelled` mean what
  `job.status` meant; `timed_out` is reported as `cancelled`; anything else, or nothing, is
  reported as a failure.
- **What the job definition did is the runner's to provide:** one deploy at a time on the
  host, with pending manual requests kept; a timeout that bounds the idle wait (480
  minutes there); manual runs only from `main`; and the security invariant at the top of
  `deploy.yml`, that no job on this host is reachable from a fork-triggerable event.
- **A host that still has the pre-rename home** gets no dedicated message from this entry
  point: with no Lantern home to deploy to, `prepare` fails at its first helper call
  instead, before anything is touched.

### What it does not cover

`finish` runs only if the runner runs it. If the runner's own process is killed outright,
or the host goes down mid-job, nothing after that point happens: the hold stays, and the
daemon stays paused, until someone releases it by hand.

```bash
lantern daemon ctl status                          # the standing holds, and whose
lantern daemon ctl resume --hold deploy-<run id>   # with the prefix, if one was set
```

If the upgrade had already started, `state/deploy.json` still says `in_progress` as well:
check the host, then run a manual deploy to verify health and clear it, as after any
interrupted deployment. The `deploy-daemon-<run id>` directory left under `RUNNER_TEMP`
holds only wheels and the job's own notes, and can be removed.

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
- **The deploy moved to another runner.** `deploy.yml` lost its `workflow_run` and
  `schedule` triggers and became the manual break-glass; a job on another runner starts
  the deploy when a release is published
  ([Running the deploy from another runner](#running-the-deploy-from-another-runner)).
  The one manual step is the order: have that job ready before this change merges,
  because from the merge on nothing in this repository starts a deploy. A release
  published in between is not lost: every deploy selects the newest complete release.
