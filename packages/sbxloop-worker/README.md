# sbxloop-worker

> **sbxloop is now Lantern.** This project continues as **Lantern backend** at
> <https://github.com/brettbergin/lantern-backend>. This is the final release
> published to PyPI under this name: Lantern installs from its GitHub Releases,
> with the `lantern` CLI, `LANTERN_*` environment and `~/.lantern` home.

The in-sandbox runtime for [sbxloop](https://github.com/brettbergin/lantern-backend):
shared host/worker protocol models, the job runner (`python -m sbxloop_worker`),
and agent backends.

You never install this directly — the sbxloop host package carries this wheel
and provisions it into sandboxes itself (with the `copilot` extra in agent
sandboxes). It is never installed by name from a package index.

The `claude` and `codex` extras select the other agent backends. `codex`
installs the Python `openai-codex` SDK and its matching Codex CLI runtime;
it does not require Node. The worker uses `OPENAI_API_KEY` and exposes its
own governed local tools plus the host's event/file tool relay. See the
[Codex backend design](../../docs/codex-backend.md) for capabilities and
verification limits.
