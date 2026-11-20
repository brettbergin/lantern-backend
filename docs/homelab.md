# Lantern in the maintainer's homelab

**Homelab only.** This page is for agents and people working on the maintainer's own
deployment of Lantern. Nothing here is part of the product: the generic procedure is
[deploy.md](deploy.md), and the facts about this repository's own daemon host are
[self-deploy.md](self-deploy.md). `AGENTS.md` points here so that its product guidance stays
free of host details (`scripts/check_self_references.py` keeps them out of it).

- Tier: D, host service (the Lantern daemon on db.srv, the user unit `lantern-daemon`, not on
  k3s).
- Name: `lantern-backend` (repository). The berg-runner pipeline that deploys it is
  `deploy-lantern-daemon`.
- Standard: `brettbergin/berg-net-pi-suite` `docs/standard/app-standard.md` (Tier D section),
  `operate.md`, `exceptions.md`. Load the `homelab-app-standard` skill
  (`.claude/skills/homelab-app-standard/`, a loader that reads the current standard from
  pi-suite `main`) before changing deploys, wiring or operations.
- Sanctioned exception: EX-3, it runs on db.srv under systemd, keeps SQLite and publishes its
  releases to GitHub Releases, because it drives `sbx` microVMs that need the host. CI is not
  exempt.
- Open gaps (pi-suite issue "Uniform homelab: tracking"):
  - LB-1: CI and release run on GitHub Actions; the standard's CI is berg-runner.
  - LB-2: no Prometheus metrics; whether a GlitchTip DSN is set in `lantern.toml` is unknown.
  - P-05: error tracking goes to GlitchTip (decision D-2), not wired here yet.

## Commands

The gates are the ones in `AGENTS.md`, "Working here". `make check` runs `lint`, `typecheck`,
`security` and `test-cov`.

## Deploy and operate

- A merge to `main` becomes a GitHub Release `v*` through the `Release` workflow
  ([RELEASING.md](../RELEASING.md)).
- berg-runner's `deploy-lantern-daemon` (pi-suite
  `berg-runner/pipelines/deploy-lantern-daemon.yaml`) is the only deploy. It runs on the agent
  `db` as the Lantern account, for each published release and nightly at 05:23 Pacific, and
  posts a thread to the homelab deploy channel. It takes a hold, waits for the daemon to go
  idle, upgrades, restarts, health-checks and rolls back on a failure
  ([self-deploy.md](self-deploy.md)).
- By hand: a manual run of `deploy-lantern-daemon`, input `version` (blank for the latest
  complete release), from the berg-runner UI or the berg-runner MCP server (`start_run`,
  `wait_run`).
- Logs, alerts and rollback follow pi-suite `docs/standard/operate.md` as far as a host service
  allows; host commands are in [self-deploy.md](self-deploy.md), "Operating it".

## Contracts

- `scripts/deploy_daemon.py` and `scripts/release_pipeline.py` are fetched from `main` by
  `deploy-lantern-daemon` at run time. Their subcommands (`prepare --wait-cooldown`, `deploy`,
  `finish --status`) and environment (`INPUT_VERSION`, `DEPLOY_TOKEN_ENV`, `HOST`, `UNIT`,
  `DEPLOY_CHANNEL`, `DEPLOY_HOLD_PREFIX`, `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`) are an
  interface: change them together with the pipeline.
- A release tag `v*` starts a deploy.
- The daemon's API is the backend of `brettbergin/lantern-web-app` (Tier A) and of the iOS client
  `brettbergin/lantern-mobile-app`.

## Rules

- The deploy pipeline is the only path to the host; never upgrade or edit it by hand outside the
  procedures in [self-deploy.md](self-deploy.md).
- Homelab specifics live in `docs/`, not in `AGENTS.md` or shipped files.
- This page changes in the same pull request as the deploy behaviour it describes.
