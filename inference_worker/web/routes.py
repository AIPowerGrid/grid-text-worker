import os
import logging
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import Settings
from ..detect_backends import (
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

ENV_PATH = Path.cwd() / ".env"


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
    ):
        return await call_next(request)
    if not worker_state["setup_complete"]:
        return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------
@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    detection = detect_backends()
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "detection": detection,
        "platform": get_platform(),
    })


@app.post("/api/setup/detect")
async def api_detect():
    """Scan all known ports for running inference engines."""
    detection = detect_backends()
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
    info = await check_backend_url(url)
    return info


@app.post("/api/setup/install-ollama")
async def api_install_ollama():
    """Install Ollama using the official install script."""
    result = install_ollama()
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
    """Send a greeting to the model and return its response."""
    body = await request.json()
    url = body.get("url", Settings.OLLAMA_URL).rstrip("/")
    engine = body.get("engine", "ollama")
    model = body.get("model", "")

    prompt = (
        "You're an AI model being configured for a decentralized network. "
        "Sign of life."
    )

    # Use OpenAI-compatible chat completions (works with Ollama too)
    chat_url = f"{url}/v1/chat/completions"
    if engine == "ollama":
        chat_url = f"{url}/v1/chat/completions"
    elif not url.endswith("/v1"):
        chat_url = f"{url}/v1/chat/completions"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(chat_url, json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 40,
                "temperature": 0.8,
            })
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"].strip()
                return {"ok": True, "reply": reply, "prompt": prompt}
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
    result = await get_model_context_length(url, engine, model)
    return result


@app.post("/api/setup/list-models")
async def api_list_models(request: Request):
    """List models available on any backend."""
    body = await request.json()
    url = body.get("url", Settings.OLLAMA_URL)
    engine = body.get("engine")
    models = await list_models_for_backend(url, engine)
    return {"models": models}


@app.post("/api/setup/complete")
async def api_complete_setup(request: Request):
    """Save config and start the worker."""
    form = await request.json()

    # Build .env content, preserving any existing keys
    env_lines = _read_existing_env()
    for key, value in form.items():
        if value is not None and value != "":
            env_lines[key] = value

    # Write .env
    content = "\n".join(f"{k}={v}" for k, v in env_lines.items()) + "\n"
    ENV_PATH.write_text(content)

    # Reload settings in memory
    _reload_settings(form)

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
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "worker_running": worker_state["running"],
        "worker_error": worker_state.get("error"),
    })


@app.get("/api/status")
async def api_status():
    # Grab live session stats from the worker if running
    session_stats = None
    worker = worker_state.get("worker")
    if worker and hasattr(worker, "stats"):
        session_stats = worker.stats.to_dict()

    return {
        "worker_running": worker_state["running"],
        "worker_error": worker_state.get("error"),
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
    return templates.TemplateResponse("logs.html", {"request": request})


@app.get("/api/logs")
async def api_logs():
    return {"lines": list(log_buffer)}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {
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

    env_lines = _read_existing_env()
    for key, value in form.items():
        if value is not None and value != "":
            env_lines[key] = value
        elif key in env_lines:
            del env_lines[key]

    content = "\n".join(f"{k}={v}" for k, v in env_lines.items()) + "\n"
    ENV_PATH.write_text(content)
    _reload_settings(form)

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
        # Find user (kudos, worker list)
        try:
            r = await client.get(f"{api}/v2/find_user", headers=headers)
            if r.status_code == 200:
                result["user"] = r.json()
        except Exception:
            pass

        # Grid performance
        try:
            r = await client.get(f"{api}/v2/status/performance")
            if r.status_code == 200:
                result["performance"] = r.json()
        except Exception:
            pass

        # Text stats
        try:
            r = await client.get(f"{api}/v2/stats/text/totals")
            if r.status_code == 200:
                result["text_stats"] = r.json()
        except Exception:
            pass

        # Find our worker by name
        if Settings.GRID_WORKER_NAME:
            try:
                r = await client.get(
                    f"{api}/v2/workers",
                    headers=headers,
                )
                if r.status_code == 200:
                    workers = r.json()
                    for w in workers:
                        if Settings.GRID_WORKER_NAME and \
                           w.get("name", "").startswith(Settings.GRID_WORKER_NAME):
                            result["worker"] = w
                            break
            except Exception:
                pass

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _read_existing_env() -> dict:
    """Read existing .env into an ordered dict."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _reload_settings(form: dict):
    """Update Settings class attributes from form data."""
    if "GRID_API_KEY" in form:
        Settings.GRID_API_KEY = form["GRID_API_KEY"]
    if "GRID_WORKER_NAME" in form:
        Settings.GRID_WORKER_NAME = form["GRID_WORKER_NAME"]
    if "BACKEND_TYPE" in form:
        Settings.BACKEND_TYPE = form["BACKEND_TYPE"]
    if "OLLAMA_URL" in form:
        Settings.OLLAMA_URL = form["OLLAMA_URL"]
    if "OPENAI_URL" in form:
        Settings.OPENAI_URL = form["OPENAI_URL"]
    if "OPENAI_API_KEY" in form:
        Settings.OPENAI_API_KEY = form["OPENAI_API_KEY"]
    if "MODEL_NAME" in form:
        Settings.MODEL_NAME = form["MODEL_NAME"]
    if "GRID_MODEL_NAME" in form:
        Settings.GRID_MODEL_NAME = form["GRID_MODEL_NAME"]
    if "GRID_NSFW" in form:
        Settings.NSFW = form["GRID_NSFW"].lower() == "true"
    if "GRID_MAX_THREADS" in form:
        Settings.MAX_THREADS = int(form["GRID_MAX_THREADS"])
    if "GRID_MAX_LENGTH" in form:
        Settings.MAX_LENGTH = int(form["GRID_MAX_LENGTH"])
    if "GRID_MAX_CONTEXT_LENGTH" in form:
        Settings.MAX_CONTEXT_LENGTH = int(form["GRID_MAX_CONTEXT_LENGTH"])
    if "WALLET_ADDRESS" in form:
        Settings.WALLET_ADDRESS = form["WALLET_ADDRESS"]
