---
name: homelab-app-standard
description: Load the homelab app standard before building, changing, reviewing, deploying, debugging or rolling back any brettbergin homelab app or its berg-net-pi-suite wiring. Covers tiers, required layout, auth, UI, deploy path, observability and exceptions.
---

# Homelab app standard (loader)

The canonical standard lives in `brettbergin/berg-net-pi-suite` under
`docs/standard/`. This skill is a loader: read the current files, never a
remembered copy. Local checkouts can be stale; prefer the remote `main`.

```bash
gh api repos/brettbergin/berg-net-pi-suite/contents/docs/standard/app-standard.md \
  -H 'Accept: application/vnd.github.raw'
```

Read on demand:

| File | When |
| --- | --- |
| `app-standard.md` | Building, changing or reviewing an app |
| `operate.md` | Something is broken; logs, alerts, rollback |
| `exceptions.md` | An app deviates; check before "fixing" it |
| `decisions.md` | Why a rule exists |

Progress and finding IDs (P-01, MN-1, ...) are in the pi-suite issue titled
"Uniform homelab: tracking". Name the IDs a change closes in its PR.

## Procedure

1. Identify the app's tier (A web service, B job, C third-party, D host
   service) and its one name `<app>`; read the app repo's `AGENTS.md`.
2. Check [the quick map](references/quick-map.md) for where each layer lives.
3. Before changing a deviation, look it up in `exceptions.md`. A listed
   exception is intended; anything else is a gap to fix toward the standard.
4. New or changed code follows the reference app `brettbergin/repos` unless
   the standard says otherwise. Never copy a deviation into a new app.
5. Deploys go only through `deploy-<app>` on berg-runner; roll back by a
   manual run with `sha` and `allow_rollback=true`. Never hand-edit the cluster
   or the control node checkout.
6. Docs, `AGENTS.md` and the standard change in the same PR as the behavior.
