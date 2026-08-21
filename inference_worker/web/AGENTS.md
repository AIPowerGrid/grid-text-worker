# inference_worker/web — setup wizard + control dashboard

## Purpose

The local browser UI at `http://localhost:7861`: a first-run setup wizard (detect backend,
pick/pull a model, test it, save config) and an ongoing dashboard (status, live stats, logs,
settings, worker start/stop/restart). FastAPI app that owns and supervises the worker task.

## Ownership

- `app.py` — `FastAPI` app + `lifespan`: starts/stops the worker task by calling
  `ws_client.run_workers`, captures logs into a 500-line ring buffer.
  Holds `worker_state` (shared with routes).
- `routes.py` — all HTTP endpoints + two middlewares: `setup_guard` (redirect to `/setup`
  until configured) and `auth_guard` (dashboard token via cookie / Bearer / `?token=`).
  `/setup` wizard APIs (`/api/setup/*`), dashboard APIs (`/api/status`, `/api/logs`,
  `/api/settings`, `/api/worker/restart`, `/api/grid-stats`), `/login`.
- `templates/` (Jinja2), `static/` — UI assets, bundled into the PyInstaller build.

## Local Contracts

- **Worker lifecycle lives here, not in the worker classes.** Start/stop/restart go through
  `start_worker`/`stop_worker`; never spawn a worker task elsewhere.
- **Every page/API except `_AUTH_EXEMPT` (`/static`, `/login`, `/favicon.ico`) requires the
  dashboard token.** Keep new endpoints behind `auth_guard`; `/api/*` returns 401 JSON.
- The dashboard binds to loopback by default. LAN exposure requires the explicit
  `--host 0.0.0.0` operator choice. A valid `?token=` bootstrap is immediately
  exchanged for a strict, HTTP-only cookie and removed from the browser URL.
- Operator-supplied backend URLs may target loopback, private LAN, or public
  inference services, but must pass `validated_backend_url` before probing or
  persistence. Cloud metadata, link-local, multicast, reserved, credentialed,
  query-bearing, and malformed targets are forbidden.
- Management APIs return stable error classes rather than raw exceptions or
  backend bodies. Automatic remote-script installation is not part of this UI;
  operators install Ollama through its reviewed platform installer.
- **Don't run blocking detection in request handlers** — `/setup` renders instantly and the
  page calls `POST /api/setup/detect`; wrap blocking probes in `asyncio.to_thread`.
- Persist config only via `env_utils.write_env` + `reload_settings`; `Settings` is the single source.

## Work Guidance

—

## Verification

- Boot `grid-inference-worker --no-gui`; open the printed `?token=` URL; complete the wizard
  against a local backend and confirm the dashboard shows the worker running.

## Child DOX Index

- None — leaf.
