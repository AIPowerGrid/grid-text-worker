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
  Theme = the AIPG **console theme**: token values in `static/style.css` are ported from
  grid-frontend `src/app/globals.css` (dark), AIPG orange `#f8991d` primary, and a
  Lato-first local system-font stack. Keep the two palettes in sync when the console rebrands.
  `static/vendor/` holds the exact Alpine.js runtime and its upstream license; the local
  management UI must not execute remotely hosted scripts or depend on web fonts.
  `base.html` owns the app shell: a 256px left sidebar (brand, nav, worker chip footer)
  beside a fluid column with a 64px breadcrumb topbar that carries the live status pill,
  Restart, and error banners (`shellStatus()` polls `/api/status` on every page). Pages
  extend it via blocks: `crumb`, `content`, `main_class`, plus `shell_nav`/`topbar`/
  `shell_attrs`/`shell_script` which the setup wizard blanks to stay a centered flow.

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
- Browser runtime dependencies are versioned local assets included in the frozen
  binary. Do not replace them with floating CDN URLs; update the vendored file,
  license, and `static/vendor/README.md` provenance together.
- **Don't run blocking detection in request handlers** — `/setup` renders instantly and the
  page calls `POST /api/setup/detect`; wrap blocking probes in `asyncio.to_thread`.
- Persist config only via `env_utils.write_env` + `reload_settings`; `Settings` is the single source.
- Setup and Settings accept only their explicit backend-field allowlist. Keep
  values bounded and canonicalized before writing `.env`; the local manager is
  not a general environment-variable editor.
- `/api/status` separates process lifetime (`worker_running`) from confirmed
  registration (`grid_connected`, `connected_workers`, `total_workers`). Online
  requires every active connection to have a Grid `ready` handshake. Poll failure
  makes status unavailable rather than preserving a stale Online badge.
- Session counters aggregate every active connection; no singular worker slot is
  authoritative in a multi-backend process.
- Dashboard den labels describe accepted work accounting. `den_per_hour` is a
  process-lifetime operational rate, not a token balance, conversion rate, or
  payout forecast. The two hourly rate cards are rounded to one decimal and
  snapshot the first status response for the page; refresh the page to update
  them instead of letting elapsed-time polling make them visibly drift.
- Setup waits for confirmed registration after saving. A rejection or bounded
  wait failure exposes Logs, never a timed success animation. Retrying setup
  restarts the previous worker so corrected credentials actually take effect.
- API-key and payout-wallet setup link to the console. Do not request wallet
  connections locally or claim the legacy wallet field controls rewards.
- Setup and Settings expose the single-backend concurrency limit and validated
  local-time schedule. Schedule JSON is bounded and canonicalized before
  persistence; Console-enrolled credentials may schedule only zero or one
  connection. Empty Settings input clears the active schedule.
- `/api/setup/enrollment/start` and `/api/setup/enrollment/poll` drive the
  default Console device flow. They may return public approval state only;
  candidate keys, poll tokens, delegation files, and dashboard tokens must
  never enter template state or JSON responses. An error retry explicitly
  replaces stale pending state. Manual account keys remain an Advanced option.
- The native manager may copy the authenticated dashboard link only after an
  explicit operator action. Ordinary startup logs show the token-free local URL;
  the web dashboard and status APIs never render or return the token.

## Work Guidance

—

## Verification

- Boot `grid-inference-worker --no-gui`; open the printed `?token=` URL; complete the wizard
  against a local backend and confirm Grid registration, not just a running task.
- Test an invalid API key and unavailable Grid endpoint: neither may display
  successful setup/Online. Also verify a clean stop clears all child connections.
- `node --test tests/onboarding-ui.test.mjs` checks wizard and status transitions
  without contacting a backend or Grid.

## Child DOX Index

- None — leaf.
