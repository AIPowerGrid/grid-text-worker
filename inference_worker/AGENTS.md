# inference_worker — worker package (transport + backend bridge)

## Purpose

The worker runtime: connect to the grid, pop/receive text jobs, transform them to OpenAI
`/chat/completions`, run them against the local backend, and return generations. Plus the
launcher (CLI/GUI), backend detection, config, and cross-platform service install.

## Ownership

- **Grid transport:**
  - `ws_client.py` — `StreamingWorker` + `run_workers`: persistent WebSocket to
    `/v1/workers/ws`, the only transport.
    Registers, receives pushed jobs, streams tokens back live, awaits a `done` ack with `den`
    reward, and honors `cancel` frames that abort the in-flight backend request. Reasoning
    models stream `<think>…</think>` live. `GRID_BACKENDS` runs one worker per backend under a
    single supervisor; declared `input_modalities` (vision) surface on grid `/v1/models`.
  - `p2p_client.py` — experimental libp2p/gossipsub transport (`P2P_ENABLED`, runs trio).
- **Backend bridge:** `prompts.py` owns shared local-backend prompts and response
  cleanup. `detect_backends.py` owns port scans, model/context probes, and
  Ollama installation.
- **Config / launch:** `config.py` (`Settings`, per-machine default worker name, stable config
  dir), `env_utils.py` (.env read/write + dashboard token), `cli.py` (argparse entry, GUI vs
  console), `gui.py` (Tkinter window), `headless.py` (terminal quick-setup), `service.py`
  (systemd / launchd / Windows-startup install).
- `web/` — browser setup wizard + dashboard. Owned in its own AGENTS.md.

## Local Contracts

- **Grid model name** is the advertised id (`grid/<model>` or `openai/<model>`); the
  **backend model name** (`MODEL_NAME`) is what the local engine serves. Do not conflate.
- **Always submit a result** (even empty/faulted) so a job never hangs in the grid; mark
  `state="faulted"` on backend validation errors.
- **Thinking tags:** the streaming path surfaces reasoning live wrapped in `<think>…</think>`
  and always closes an open block.
- Transport errors back off with bounded exponential delay; 401 surfaces as `api_auth_error`.
- `service.py` uses `sys.executable` (not pip wrappers), `shlex.quote`s runtime paths, and
  writes units via secure temp files — keep these invariants.
- `detect_backends.validated_backend_url` is the shared management-plane URL
  boundary. Preserve support for operator-owned loopback, LAN, and public
  backends while always rejecting cloud metadata and special-purpose targets.

## Work Guidance

- Job-shape changes land in `ws_client.py`, the only live transport.
- New backend engine → add a probe entry in `detect_backends.KNOWN_ENGINES`.

## Verification

- `pytest` from repo root (smoke tests).
- Manual: point at a local Ollama, run `grid-inference-worker`, confirm jobs complete in the dashboard.

## Child DOX Index

- [web/AGENTS.md](web/AGENTS.md) — FastAPI setup wizard + control dashboard.
