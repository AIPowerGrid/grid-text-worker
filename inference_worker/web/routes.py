import logging
import urllib.parse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config import Settings
from ..env_utils import ENV_PATH, read_env, write_env, reload_settings
from ..worker import ENLISTMENT_PROMPT, strip_thinking_tags
from ..detect_backends import (
    DetectionResult,
    detect_backends,
    check_backend_url,
    list_models_for_backend,
    get_model_context_length,
    install_ollama,
    pull_ollama_model,
    get_platform,
)
from .app import app, templates, worker_state, log_buffer, start_worker, stop_worker

logger = logging.getLogger(__name__)

_AUTH_EXEMPT = ("/static", "/login", "/favicon.ico")


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
        response = await call_next(request)
        response.set_cookie(
            "_token", token, httponly=True, samesite="lax", max_age=86400 * 365,
        )
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
    next_url = request.query_params.get("next", "/")
    return templates.TemplateResponse(request, "login.html", {"request": request, "next": next_url})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    token = form.get("token", "")
    next_url = form.get("next", "/")
    if token == Settings.DASHBOARD_TOKEN:
        response = RedirectResponse(next_url, status_code=303)
        response.set_cookie(
            "_token", token, httponly=True, samesite="lax", max_age=86400 * 365,
        )
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


@app.post("/api/setup/install-ollama")
async def api_install_ollama():
    """Install Ollama using the official install script."""
    import asyncio
    result = await asyncio.to_thread(install_ollama)
    return result


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
    url = req_body.get("url", Settings.OLLAMA_URL).rstrip("/")
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
        "max_tokens": 80,
        "temperature": 0.8,
    }
    if engine == "ollama":
        payload["think"] = False

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
                    data = resp.json()
                    choice = data.get("choices", [{}])[0]
                    reply = (choice.get("message", {}).get("content") or "").strip()
                    reply = strip_thinking_tags(reply)
                    if choice.get("finish_reason") == "length":
                        reply += " …"
                    return {"ok": True, "reply": reply, "prompt": prompt}
                if resp.status_code in (400, 503) and attempt < 2:
                    await asyncio.sleep(5)
                    continue
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
    form = await request.json()

    write_env(form)
    reload_settings(form)

    worker_state["setup_complete"] = True

    # Start worker
    if Settings.GRID_API_KEY and Settings.MODEL_NAME:
        await start_worker()

    logger.info("Setup complete. Worker starting.")
    return {"ok": True}


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
    session_stats = None
    api_auth_error = None
    worker = worker_state.get("worker")
    if worker and hasattr(worker, "stats"):
        session_stats = worker.stats.to_dict()
    if worker and hasattr(worker, "api_auth_error"):
        api_auth_error = worker.api_auth_error

    return {
        "worker_running": worker_state["running"],
        "worker_error": worker_state.get("error"),
        "api_auth_error": api_auth_error,
        "session_stats": session_stats,
        "config": {
            "has_api_key": bool(Settings.GRID_API_KEY),
            "worker_name": Settings.GRID_WORKER_NAME,
            "backend_type": Settings.BACKEND_TYPE,
            "ollama_url": Settings.OLLAMA_URL,
            "model_name": Settings.MODEL_NAME,
            "grid_model_name": Settings.GRID_MODEL_NAME,
            "max_threads": Settings.MAX_THREADS,
            "max_length": Settings.MAX_LENGTH,
            "max_context_length": Settings.MAX_CONTEXT_LENGTH,
            "nsfw": Settings.NSFW,
            "wallet_address": Settings.WALLET_ADDRESS,
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
            "GRID_API_KEY": Settings.GRID_API_KEY,
            "GRID_WORKER_NAME": Settings.GRID_WORKER_NAME,
            "BACKEND_TYPE": Settings.BACKEND_TYPE,
            "OLLAMA_URL": Settings.OLLAMA_URL,
            "OPENAI_URL": Settings.OPENAI_URL,
            "OPENAI_API_KEY": Settings.OPENAI_API_KEY,
            "MODEL_NAME": Settings.MODEL_NAME,
            "GRID_MODEL_NAME": Settings.GRID_MODEL_NAME,
            "GRID_NSFW": str(Settings.NSFW).lower(),
            "GRID_MAX_THREADS": str(Settings.MAX_THREADS),
            "GRID_MAX_LENGTH": str(Settings.MAX_LENGTH),
            "GRID_MAX_CONTEXT_LENGTH": str(Settings.MAX_CONTEXT_LENGTH),
            "WALLET_ADDRESS": Settings.WALLET_ADDRESS,
        },
    })


@app.post("/api/settings")
async def save_settings(request: Request):
    """Save settings to .env and update in-memory config."""
    form = await request.json()

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
    api = Settings.GRID_API_URL.rstrip("/")
    headers = {"apikey": Settings.GRID_API_KEY} if Settings.GRID_API_KEY else {}
    result = {"user": None, "worker": None, "performance": None, "text_stats": None}

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{api}/v2/find_user", headers=headers)
            if r.status_code == 200:
                result["user"] = r.json()
        except Exception:
            pass

        try:
            r = await client.get(f"{api}/v2/status/performance")
            if r.status_code == 200:
                result["performance"] = r.json()
        except Exception:
            pass

        try:
            r = await client.get(f"{api}/v2/stats/text/totals")
            if r.status_code == 200:
                result["text_stats"] = r.json()
        except Exception:
            pass

        if Settings.GRID_WORKER_NAME:
            try:
                r = await client.get(f"{api}/v2/workers", headers=headers)
                if r.status_code == 200:
                    workers = r.json()
                    for w in workers:
                        if w.get("name", "").startswith(Settings.GRID_WORKER_NAME):
                            result["worker"] = w
                            break
            except Exception:
                pass

    return result
