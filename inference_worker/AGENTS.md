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
- **Worker identity / enrollment:** `worker_identity.py` owns the funds-less rig
  signer, payout-wallet delegation verification, and capability-bound WS proof.
  `enrollment.py` owns the crash-resumable Console approval flow and installs an
  exact-name worker-only API key without returning it to browser JavaScript.
- `web/` — browser setup wizard + dashboard. Owned in its own AGENTS.md.

## Local Contracts

- **Grid model name** is the advertised id (`grid/<model>` or `openai/<model>`); the
  **backend model name** (`MODEL_NAME`) is what the local engine serves. Do not conflate.
- **Always submit a result** (even empty/faulted) so a job never hangs in the grid; mark
  `state="faulted"` on backend validation errors.
- **Thinking tags:** the streaming path surfaces reasoning live wrapped in `<think>…</think>`
  and always closes an open block.
- Transport errors back off with bounded exponential delay. `connected` becomes
  true only after a valid Grid `ready` frame with a worker ID. Disconnect,
  cancellation, and close clear that state before cleanup/backoff. A bounded
  `connection_error` reaches the dashboard without remote response bodies.
- The supervisor optionally exposes its active workers to the dashboard and
  awaits child cleanup before returning, including scheduled scale-down.
- `eth-account` is a required runtime dependency. Every shipped worker signs
  result receipts; release binaries must pass `--verify-runtime` before staging.
- Secure Console enrollment is the default for a single backend with one
  connection. It binds the API key, delegation, worker signer, and exact worker
  name. Multi-backend or parallel operators use the explicit advanced account
  API-key path until Core supports a safely scoped set of connection names.
- Enrollment state and the rig signer are local secrets: write atomically with
  mode `0600` where supported, reject symlinks, require HTTPS off loopback, and
  never return candidate API keys or poll tokens through the local web API.
- Grid resolves the payout wallet from the authenticated account. The legacy
  local `WALLET_ADDRESS` is not payout authority and must not be presented as such.
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
