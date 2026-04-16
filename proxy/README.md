# proxy/ — the OtoDock platform core

A FastAPI service that is the heart of the platform: it owns agent sessions
(spawning real Claude Code / Codex processes inside per-session kernel
sandboxes), the security model (auth, roles, path policy, permission
prompts), scheduling and triggers, MCP tool wiring, file/document handling,
and the WebSocket hub the dashboard streams from. PostgreSQL is its only
required backing service.

- Entry point: `app.py` (serves the API, the WebSockets, and the built
  dashboard).
- Tests: `tests/` — see [`tests/README.md`](tests/README.md) for the
  canonical commands (`pytest -n 8` + a reachable Postgres).
- Configuration: generated `config.env` at the repo root;
  [`../config.env.example`](../config.env.example) documents every knob.

Architecture and feature deep-dives live in the developer docs at
[docs.otodock.io](https://docs.otodock.io).
