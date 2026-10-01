# lantern-worker

The in-sandbox runtime for [lantern](https://github.com/brettbergin/lantern-backend):
shared host/worker protocol models, the job runner (`python -m lantern_worker`),
and agent backends.

You never install this directly — the Lantern host package carries this wheel
and provisions it into sandboxes itself (with the `copilot` extra in agent
sandboxes). It is never installed by name from a package index.

The `claude` and `codex` extras select the other agent backends. `codex`
installs the Python `openai-codex` SDK and its matching Codex CLI runtime;
it does not require Node. The worker uses `OPENAI_API_KEY` and exposes its
own governed local tools plus the host's event/file tool relay. See the
[Codex backend design](../../docs/codex-backend.md) for capabilities and
verification limits.
