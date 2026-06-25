# DOX framework

- DOX is a hierarchy of AGENTS.md files that carry the durable contracts for this repo.
- Agents must follow the DOX chain on every edit.

## Core Contract

- AGENTS.md files are binding work contracts for their subtrees.
- Any work product must stay understandable from the nearest AGENTS.md plus every parent above it.

## Read Before Editing

1. Read this root AGENTS.md.
2. Identify every path you expect to touch.
3. Walk from repo root to each target, reading every AGENTS.md on the way.
4. The nearest AGENTS.md is the local contract; parents hold repo-wide rules.
5. If docs conflict, the closer doc controls local detail, but no child may weaken DOX.

Do not rely on memory — re-read the applicable chain in-session before editing.

## Update After Editing

Every meaningful change requires a DOX pass before the task is done. Update the closest
owning AGENTS.md when a change affects: purpose/scope/ownership; durable structure,
contracts, or workflows; inputs/outputs/permissions/side-effects; or the Child DOX Index.
Remove stale text immediately. Refresh affected parent and child indexes.

## Style

Concise, current, operational. Stable contracts, not diary entries. Broad rules in parents,
concrete detail in children. Delete stale notes instead of explaining history.

---

# grid-text-worker — turn-key text inference worker

## Purpose

The end-user worker that earns on AI Power Grid. It bridges the grid coordinator to a local
OpenAI-compatible inference backend (Ollama / vLLM / SGLang / LMDeploy / LM Studio /
KoboldCpp): pops text-generation jobs, runs them through the backend, and returns the
result. Ships as a single PyInstaller binary with a browser setup wizard + dashboard at
`http://localhost:7861`. Entry point: `inference_worker/cli:main`.

## Ownership

- `inference_worker/` — the worker package: grid transport, backend bridge, config,
  detection, service install, CLI/GUI. Owned in its own AGENTS.md.
- `run_worker.py` / `run_frozen.py` — thin launchers (dev / PyInstaller entry).
- `scripts/`, `*.spec`, `Dockerfile`, `docker-compose.yml` — packaging/build (no DOX child).
- `docs/` — vLLM setup + optimization guides (Markdown, not AGENTS.md).
- `tests/` — pytest smoke tests.

## Local Contracts

- **Inherit org engineering standards:** `aipg-documentation/engineering-standards/`
  (core + `git` + the matching language file — `python.md`).
- **This is a CLIENT of the grid, not the grid.** It speaks the grid's worker protocols only;
  it never owns coordinator state. The grid's contracts live in `grid-core/grid_api`.
- **Two transports, same backend bridge:** legacy HTTP polling (`/v2/generate/text/pop` +
  `/submit`, default) and WebSocket streaming (`/v1/workers/ws`, `GRID_STREAMING=true`).
  Keep both in sync when the job shape changes. P2P (`P2P_ENABLED`) is experimental scaffolding.
- **Config is env-only** via `inference_worker/config.py:Settings`; never read env elsewhere.
- Run from `grid-core`'s prod tree on servers; this repo produces the distributable binary.

## Work Guidance

- New config → add to `config.Settings`, surface in the web setup/settings pages and README env table.
- Backend requests are OpenAI `/chat/completions`; route grid I/O through `api_client` (HTTP)
  or `ws_client` (WS), never ad-hoc.

## Verification

- `pip install -e ".[test]"` then `pytest` (smoke tests under `tests/`).
- `grid-text-worker --no-gui` should boot the dashboard at `http://localhost:7861`.

## Child DOX Index

- [inference_worker/AGENTS.md](inference_worker/AGENTS.md) — the worker package (transport, backend bridge, CLI/GUI, service).
