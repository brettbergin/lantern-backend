# Quick map for a standard app `<app>`

A pointer table only. The rules are in berg-net-pi-suite `docs/standard/`.

| Layer | Where |
| --- | --- |
| Code | `github.com/brettbergin/<app>`; `AGENTS.md`, `docs/SPEC.md`, `make check` |
| Runtime | Python 3.13, uv, FastAPI, SQLAlchemy 2, Alembic; `python -m <pkg> api/migrate/job` |
| UI | `web/`: React, TypeScript, Vite, pnpm, `@primer/react` |
| Auth | In-app OIDC + PKCE against Authentik; groups `<app>-admins`, `<app>-users` |
| Config | `<APP>_*` env; secrets in Secret `<app>` from vault file `vault_<app_>.yml` |
| Data | Postgres database on db.srv via `<APP>_DATABASE_URL`; Alembic PreSync Job |
| CI | `.berg-runner/ci.yaml`; statuses `berg-runner/python`, `/web`, `/image` |
| Deploy | pi-suite `berg-runner/pipelines/deploy-<app>.yaml`, `scripts/ci-deploy-<app>.sh`; status `homelab/deploy` |
| Manifests | pi-suite `gitops/manifests/<app>/`; Argo CD Application `<app>` |
| Logs | Loki `{namespace="<app>"}` (JSON lines) |
| Metrics, alerts | Prometheus jobs `<app>-<component>`; PrometheusRule `<app>` |
| Dashboard | Grafana uid `<app>` |
| Errors | GlitchTip project `<app>` |
| Uptime | Kuma `App: <app>` in pi-suite `kuma/monitors.yml` |
| Rollback | berg-runner manual run of `deploy-<app>` with `sha`, `allow_rollback=true` |

Tools: the berg-runner MCP server (`overview`, `list_runs`, `get_run`,
`run_log`, `start_run`, `wait_run`), the Grafana MCP server (Loki, Prometheus,
dashboards), and `ssh pihost01` for `sudo k3s kubectl` (zsh: use a function,
not a variable, for the kubectl prefix).
