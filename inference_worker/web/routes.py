import json
import logging
import re
import urllib.parse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config import Settings
from ..enrollment import EnrollmentClientError, poll_enrollment, start_enrollment
from ..env_utils import ENV_PATH, write_env, reload_settings
from ..prompts import ENLISTMENT_PROMPT, strip_thinking_tags
from ..detect_backends import (
    DetectionResult,
    detect_backends,
    check_backend_url,
    list_models_for_backend,
    get_model_context_length,
    pull_ollama_model,
    get_platform,
    validated_backend_url,
)
from .app import app, templates, worker_state, log_buffer, start_worker, stop_worker

logger = logging.getLogger(__name__)

_AUTH_EXEMPT = ("/static", "/login", "/favicon.ico")
_PERSISTED_BACKEND_SETTINGS = frozenset(
    {
        "BACKEND_TYPE",
        "GRID_API_KEY",
        "GRID_MAX_CONTEXT_LENGTH",
        "GRID_MAX_LENGTH",
        "GRID_MAX_THREADS",
        "GRID_MODEL_NAME",
        "GRID_NSFW",
        "GRID_SCHEDULE",
        "GRID_WORKER_NAME",
        "MODEL_NAME",
        "OLLAMA_URL",
        "OPENAI_API_KEY",
        "OPENAI_URL",
    }
)
_SCHEDULE_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _model_test_result(data: object) -> tuple[str, bool, str | None]:
    """Extract visible output without exposing or mistaking reasoning for an answer."""
    if not isinstance(data, dict):
        return "", False, None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", False, None
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return "", False, choice.get("finish_reason")
    reply = strip_thinking_tags(str(message.get("content") or "")).strip()
    reasoning = message.get("reasoning") or message.get("thinking")
    return reply, bool(str(reasoning or "").strip()), choice.get("finish_reason")


def _aggregate_session_stats(workers: list[object]) -> dict | None:
    snapshots = [
        worker.session_stats()
        for worker in workers
        if hasattr(worker, "session_stats")
    ]
    if not snapshots:
        return None
    uptime = max(float(item.get("uptime_seconds") or 0) for item in snapshots)
    jobs = sum(int(item.get("jobs_completed") or 0) for item in snapshots)
    den = sum(float(item.get("den_earned") or 0) for item in snapshots)
    hours = uptime / 3600
    return {
        "jobs_completed": jobs,
        "den_earned": den,
        "jobs_per_hour": jobs / hours if hours else 0.0,
        "den_per_hour": den / hours if hours else 0.0,
        "uptime_seconds": uptime,
    }


def _safe_next_url(value: object) -> str:
    parsed = urllib.parse.urlsplit(str(value or "/"))
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        return "/"
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))


def _set_auth_cookie(response, request: Request) -> None:
    response.set_cookie(
        "_token",
        Settings.DASHBOARD_TOKEN,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=86400 * 365,
        path="/",
    )


def _validated_backend_settings(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Settings payload must be an object")
    if set(value) - _PERSISTED_BACKEND_SETTINGS:
        raise ValueError("Settings payload contains unsupported fields")
    form = {}
    for key, raw in value.items():
        text = str(raw) if raw is not None else ""
        limit = 8192 if key == "GRID_SCHEDULE" else 4096
        if len(text) > limit or "\n" in text or "\r" in text:
            raise ValueError(f"Invalid value for {key}")
        form[key] = text
    for key in ("OLLAMA_URL", "OPENAI_URL"):
        if key in form and form[key]:
            form[key] = validated_backend_url(form[key])
    if "GRID_SCHEDULE" in form:
        form["GRID_SCHEDULE"] = _validated_schedule(form["GRID_SCHEDULE"])
    if "BACKEND_TYPE" in form and form["BACKEND_TYPE"] not in {"ollama", "openai"}:
        raise ValueError("Unsupported backend type")
    if "GRID_NSFW" in form and form["GRID_NSFW"].lower() not in {"true", "false"}:
        raise ValueError("GRID_NSFW must be true or false")
    for key, lower, upper in (
        ("GRID_MAX_THREADS", 1, 16),
        ("GRID_MAX_LENGTH", 64, 32768),
        ("GRID_MAX_CONTEXT_LENGTH", 256, 131072),
    ):
        if key not in form or not form[key]:
            continue
        try:
            number = int(form[key])
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if not lower <= number <= upper:
            raise ValueError(f"{key} is outside the supported range")
        form[key] = str(number)
    return form


def _validated_schedule(value: object) -> str:
    """Validate and canonicalize the local-time capacity schedule."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("Schedule must be JSON text")
    try:
        windows = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Schedule must be valid JSON") from exc
    if not isinstance(windows, list) or len(windows) > 32:
        raise ValueError("Schedule must be a list of at most 32 windows")

    for window in windows:
        if not isinstance(window, dict):
            raise ValueError("Each schedule window must be an object")
        if set(window) - {"days", "start", "end", "concurrency"}:
            raise ValueError("Schedule window contains an unknown field")
        days = str(window.get("days") or "daily").strip().lower()
        if days not in {"*", "all", "daily"}:
            for part in days.split(","):
                bounds = [item.strip() for item in part.split("-")]
                if len(bounds) not in {1, 2} or any(
                    item not in _SCHEDULE_DAYS for item in bounds
                ):
                    raise ValueError("Schedule days must use mon-sun names")
        for field in ("start", "end"):
            if field in window and not _TIME_RE.fullmatch(str(window[field])):
                raise ValueError(f"Schedule {field} must use 24-hour HH:MM")
        concurrency = window.get("concurrency")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 0 <= concurrency <= 16
        ):
            raise ValueError("Schedule concurrency must be an integer from 0 to 16")
    return json.dumps(windows, separators=(",", ":"))


def _enrolled_settings_error(form: dict) -> str | None:
    """Keep exact-name Console credentials within their issued capability."""
    enrolled_name = Settings.GRID_ENROLLED_WORKER_NAME
    if not enrolled_name:
        return None
    if form.get("GRID_WORKER_NAME", enrolled_name) != enrolled_name:
        return "Console-enrolled credentials cannot rename this worker; reconnect the rig instead"
    try:
        max_threads = int(form.get("GRID_MAX_THREADS", Settings.MAX_THREADS))
    except (TypeError, ValueError):
        return "Max Threads must be an integer"
    if max_threads != 1:
        return "Console-enrolled credentials support one connection; use an advanced account key for parallel slots"
    schedule = form.get("GRID_SCHEDULE", Settings.GRID_SCHEDULE)
    if schedule and any(
        window.get("concurrency", 1) > 1 for window in json.loads(schedule)
    ):
        return "Console-enrolled credentials support one connection; scheduled concurrency cannot exceed one"
    return None


# ---------------------------------------------------------------------------
# Middleware: redirect to setup if not configured
# ---------------------------------------------------------------------------
@app.middleware("http")
async def setup_guard(request: Request, call_next):
    path = request.url.path
    if (
        path.startswith("/static")
        or path.startswith("/api/")
        or path.startswith("/setup")
        or path == "/login"
    ):
        return await call_next(request)
    if not worker_state["setup_complete"]:
        return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Middleware: dashboard auth token
# ---------------------------------------------------------------------------
@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path

    # Always allow static assets and the login page
    if any(path.startswith(p) or path == p for p in _AUTH_EXEMPT):
        return await call_next(request)

    token = Settings.DASHBOARD_TOKEN
    if not token:
        # No token configured (shouldn't happen, but don't lock users out)
        return await call_next(request)

    # 1. Check cookie
    if request.cookies.get("_token") == token:
        return await call_next(request)

    # 2. Check Bearer header (for API clients)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == token:
        return await call_next(request)

    # 3. Check ?token= query param (sets cookie for future requests)
    if request.query_params.get("token") == token:
        clean_query = urllib.parse.urlencode(
            [(key, value) for key, value in request.query_params.multi_items() if key != "token"]
        )
        clean_url = urllib.parse.urlunsplit(("", "", path, clean_query, ""))
        response = RedirectResponse(clean_url or "/", status_code=303)
        _set_auth_cookie(response, request)
        return response

    # Unauthorized
    if path.startswith("/api/"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return RedirectResponse(f"/login?next={urllib.parse.quote(path)}")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    next_url = _safe_next_url(request.query_params.get("next", "/"))
    return templates.TemplateResponse(request, "login.html", {"request": request, "next": next_url})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    token = form.get("token", "")
    next_url = _safe_next_url(form.get("next", "/"))
    if token == Settings.DASHBOARD_TOKEN:
        response = RedirectResponse(next_url, status_code=303)
        _set_auth_cookie(response, request)
        return response
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "next": next_url, "error": "Invalid token",
    })


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------
@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    # Don't run detect_backends() here — it scans 8+ ports (3s timeout each) and blocks 30–45s.
    # The setup page calls POST /api/setup/detect on load instead; page loads instantly.
    return templates.TemplateResponse(request, "setup.html", {
        "request": request,
        "detection": DetectionResult(),
        "platform": get_platform(),
    })


@app.post("/api/setup/detect")
async def api_detect():
    """Scan all known ports for running inference engines."""
    import asyncio
    detection = await asyncio.to_thread(detect_backends)
    return {
        "found": detection.found,
        "worker_name": Settings.GRID_WORKER_NAME,
        "ollama_binary": detection.ollama_binary,
        "ollama_version": detection.ollama_version,
        "backends": [
            {
                "engine": b.engine,
                "name": b.name,
                "url": b.url,
                "models": b.models,
                "version": b.version,
                "api_type": b.api_type,
            }
            for b in detection.backends
        ],
    }


@app.post("/api/setup/check-url")
async def api_check_url(request: Request):
    """Probe a specific URL and identify the engine."""
    body = await request.json()
    url = body.get("url", "")
    api_key = body.get("api_key", "")
    info = await check_backend_url(url, api_key=api_key)
    return info


@app.post("/api/setup/pull-model")
async def api_pull_model(request: Request):
    """Pull an Ollama model."""
    body = await request.json()
    url = body.get("url", Settings.OLLAMA_URL)
    model = body.get("model", "")
    if not model:
        return {"ok": False, "error": "No model name provided"}
    result = await pull_ollama_model(url, model)
    return result


@app.post("/api/setup/test-model")
async def api_test_model(request: Request):
    """Send an enlistment prompt to the model and return its response."""
    import asyncio
    import httpx

    req_body = await request.json()
    try:
        url = validated_backend_url(req_body.get("url", Settings.OLLAMA_URL))
    except ValueError:
        return {"ok": False, "error": "Invalid backend URL"}
    engine = req_body.get("engine", "ollama")
    model = req_body.get("model", "")
    api_key = req_body.get("api_key", "")

    prompt = ENLISTMENT_PROMPT.format(model=model)

    chat_url = f"{url}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
        "temperature": 0.2,
    }
    if engine == "ollama":
        # Ollama's OpenAI-compatible endpoint maps this to its native Think=false.
        # The native `think` field is ignored on /v1/chat/completions.
        payload["reasoning_effort"] = "none"

    # Generous timeout — first request may trigger cold model loading (30-60s)
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            for attempt in range(3):
                try:
                    resp = await client.post(chat_url, json=payload, headers=headers)
                except httpx.ReadTimeout:
                    if attempt < 2:
                        await asyncio.sleep(3)
                        continue
                    return {"ok": False, "error": "Model loading timed out — try again once the model is loaded"}
                if resp.status_code == 200:
                    reply, reasoning_only, finish_reason = _model_test_result(resp.json())
                    if not reply:
                        error = (
                            "Model used the test budget for reasoning without producing a visible reply. "
                            "The backend is reachable; check its reasoning settings or output limit."
                            if reasoning_only
                            else "Model returned an empty reply. Check the model template and output limit."
                        )
                        return {"ok": False, "error": error, "prompt": prompt}
                    if finish_reason == "length":
                        reply += " …"
                    return {"ok": True, "reply": reply, "prompt": prompt}
                if resp.status_code == 503 and attempt < 2:
                    await asyncio.sleep(5)
                    continue
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
    except Exception:
        logger.exception("Backend model test failed")
        return {"ok": False, "error": "Backend model test failed"}


@app.post("/api/setup/context-length")
async def api_context_length(request: Request):
    """Detect model context length from the backend."""
    body = await request.json()
    url = body.get("url", Settings.OLLAMA_URL)
    engine = body.get("engine")
    model = body.get("model", "")
    api_key = body.get("api_key", "")
    result = await get_model_context_length(url, engine, model, api_key=api_key)
    return result


@app.post("/api/setup/list-models")
async def api_list_models(request: Request):
    """List models available on any backend."""
    body = await request.json()
    url = body.get("url", Settings.OLLAMA_URL)
    engine = body.get("engine")
    api_key = body.get("api_key", "")
    models = await list_models_for_backend(url, engine, api_key=api_key)
    return {"models": models}


@app.post("/api/setup/complete")
async def api_complete_setup(request: Request):
    """Save config and start the worker."""
    try:
        form = _validated_backend_settings(await request.json())
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid backend settings"}, status_code=400)
    if error := _enrolled_settings_error(form):
        return JSONResponse({"ok": False, "error": error}, status_code=400)

    write_env(form)
    reload_settings(form)

    worker_state["setup_complete"] = True

    # A corrected setup must replace any reconnecting worker using the old key.
    await stop_worker()
    if Settings.GRID_API_KEY and Settings.MODEL_NAME:
        await start_worker()

    logger.info("Setup complete. Worker starting.")
    return {"ok": True}


@app.post("/api/setup/enrollment/start")
async def api_start_enrollment(request: Request):
    """Create or resume a worker-only credential approval in Console."""
    body = await request.json()
    try:
        result = await start_enrollment(
            grid_api_url=Settings.GRID_API_URL,
            worker_name=str(body.get("worker_name") or ""),
            restart=bool(body.get("restart", False)),
        )
    except EnrollmentClientError as exc:
        logger.warning("worker enrollment start failed: %s", exc)
        return JSONResponse(
            {
                "ok": False,
                "error": "Could not create worker approval. Check the Grid connection and try again.",
            },
            status_code=400,
        )
    return {"ok": True, **result}


@app.post("/api/setup/enrollment/poll")
async def api_poll_enrollment():
    """Advance the local enrollment without exposing its candidate key."""
    try:
        result = await poll_enrollment()
    except EnrollmentClientError as exc:
        logger.warning("worker enrollment poll failed: %s", exc)
        return JSONResponse(
            {
                "ok": False,
                "error": "Could not finish worker approval. Start a new Console connection and try again.",
            },
            status_code=400,
        )
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "worker_running": worker_state["running"],
        "worker_error": worker_state.get("error"),
    })


@app.get("/api/status")
async def api_status():
    workers = list(worker_state["workers"].values()) if worker_state["running"] else []
    expected = set(worker_state["expected_workers"]) if worker_state["running"] else set()
    connected = sum(w.connected for w in workers)
    connection_error = next((w.connection_error for w in workers if w.connection_error), None)

    return {
        "worker_running": worker_state["running"],
        "grid_connected": bool(expected) and connected == len(expected),
        "connected_workers": connected,
        "total_workers": len(expected),
        "connection_error": connection_error,
        "worker_error": worker_state.get("error"),
        "session_stats": _aggregate_session_stats(workers),
        "config": {
            "has_api_key": bool(Settings.GRID_API_KEY),
            "worker_name": Settings.GRID_WORKER_NAME,
            "backend_type": Settings.BACKEND_TYPE,
            "ollama_url": Settings.OLLAMA_URL,
            "model_name": Settings.MODEL_NAME,
            "grid_model_name": Settings.GRID_MODEL_NAME,
            "max_threads": Settings.MAX_THREADS,
            "schedule": Settings.GRID_SCHEDULE,
            "max_length": Settings.MAX_LENGTH,
            "max_context_length": Settings.MAX_CONTEXT_LENGTH,
            "nsfw": Settings.NSFW,
        },
    }


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse(request, "logs.html", {"request": request})


@app.get("/api/logs")
async def api_logs():
    return {"lines": list(log_buffer)}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "request": request,
        "settings": {
            "HAS_GRID_API_KEY": bool(Settings.GRID_API_KEY),
            "GRID_ENROLLED_WORKER_NAME": Settings.GRID_ENROLLED_WORKER_NAME,
            "GRID_WORKER_NAME": Settings.GRID_WORKER_NAME,
            "BACKEND_TYPE": Settings.BACKEND_TYPE,
            "OLLAMA_URL": Settings.OLLAMA_URL,
            "OPENAI_URL": Settings.OPENAI_URL,
            "HAS_OPENAI_API_KEY": bool(Settings.OPENAI_API_KEY),
            "MODEL_NAME": Settings.MODEL_NAME,
            "GRID_MODEL_NAME": Settings.GRID_MODEL_NAME,
            "GRID_NSFW": str(Settings.NSFW).lower(),
            "GRID_MAX_THREADS": str(Settings.MAX_THREADS),
            "GRID_SCHEDULE": Settings.GRID_SCHEDULE,
            "GRID_MAX_LENGTH": str(Settings.MAX_LENGTH),
            "GRID_MAX_CONTEXT_LENGTH": str(Settings.MAX_CONTEXT_LENGTH),
        },
    })


@app.post("/api/settings")
async def save_settings(request: Request):
    """Save settings to .env and update in-memory config."""
    try:
        form = _validated_backend_settings(await request.json())
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid backend settings"}, status_code=400)
    if error := _enrolled_settings_error(form):
        return JSONResponse({"ok": False, "error": error}, status_code=400)

    # Empty secret inputs mean "keep the stored value". Stored credentials are
    # never serialized into Settings-page HTML merely to support editing.
    for secret_name in ("GRID_API_KEY", "OPENAI_API_KEY"):
        if not form.get(secret_name):
            form.pop(secret_name, None)

    write_env(form, delete_empty=True)
    reload_settings(form)

    logger.info(f"Settings saved to {ENV_PATH}")
    return {"ok": True, "message": "Restart worker to apply all changes."}


@app.post("/api/worker/restart")
async def restart_worker():
    """Stop and restart the worker with current config."""
    await stop_worker()
    await start_worker()
    return {"ok": True}


@app.get("/api/grid-stats")
async def api_grid_stats():
    """Fetch worker + grid stats from the AIPG API."""
    import httpx
    origin = Settings.GRID_API_URL.rstrip("/")
    api = origin if origin.endswith("/v1") else f"{origin}/v1"
    headers = {"apikey": Settings.GRID_API_KEY} if Settings.GRID_API_KEY else {}
    result = {"user": None, "worker": None, "performance": None, "text_stats": None}

    async with httpx.AsyncClient(timeout=10) as client:
        workers_payload = {}
        try:
            r = await client.get(f"{api}/workers")
            if r.status_code == 200:
                workers_payload = r.json()
                workers = workers_payload.get("workers", [])
                result["worker"] = next(
                    (
                        worker
                        for worker in workers
                        if worker.get("name", "").startswith(Settings.GRID_WORKER_NAME)
                    ),
                    None,
                )
                result["performance"] = {
                    "text_worker_count": sum(
                        "text" in (worker.get("job_types") or ["text"])
                        for worker in workers
                    ),
                    "queued_text_requests": None,
                    "past_minute_tokens": 0,
                }
        except Exception:
            pass

        try:
            r = await client.get(f"{api}/stats/totals")
            if r.status_code == 200:
                totals = r.json()

                def text_period(name):
                    row = (totals.get(name) or {}).get("text") or {}
                    return {
                        "requests": row.get("jobs", 0),
                        "tokens": row.get("units", 0),
                        "den": row.get("den", 0),
                    }

                result["text_stats"] = {
                    "day": text_period("day"),
                    "month": text_period("month"),
                    "total": text_period("total"),
                }
        except Exception:
            pass

        try:
            r = await client.get(f"{api}/stats/models", params={"period": "minute"})
            if r.status_code == 200:
                token_rate = sum(
                    int(model.get("units") or 0)
                    for model in r.json().get("models", [])
                    if model.get("type") == "text"
                )
                if result["performance"] is not None:
                    result["performance"]["past_minute_tokens"] = token_rate
        except Exception:
            pass

        # An ordinary account key may read these; a narrowly scoped worker key
        # correctly receives 403 and still gets the public network panel above.
        try:
            account = await client.get(f"{api}/account", headers=headers)
            account_workers = await client.get(f"{api}/account/workers", headers=headers)
            if account.status_code == 200:
                result["user"] = account.json()
            if account_workers.status_code == 200:
                owned = account_workers.json()
                if result["user"] is not None:
                    result["user"]["worker_count"] = owned.get("count", 0)
                    result["user"]["den_earned"] = owned.get("den_earned", 0)
                detail = next(
                    (
                        worker
                        for worker in owned.get("workers", [])
                        if worker.get("name", "").startswith(Settings.GRID_WORKER_NAME)
                    ),
                    None,
                )
                if detail:
                    result["worker"] = {**(result["worker"] or {}), **detail}
        except Exception:
            pass

    return result
