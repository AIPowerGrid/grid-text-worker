"""Headless mode — interactive quick setup + background worker loop."""

import asyncio
import getpass
import json
import sys
import textwrap

from .config import Settings
from .env_utils import ENV_PATH, is_configured, write_env, reload_settings
from .worker import ENLISTMENT_PROMPT, strip_thinking_tags
from . import service


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9.]+", "-", s).strip("-").lower()[:32] or "model"


def _norm_openai_base(url: str) -> str:
    """Strip a trailing /v1 (and slashes) so we have a clean base to probe."""
    u = url.strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3]
    return u


def _validate_backend(base_url: str, engine: str, model: str, api_key: str) -> tuple[bool, str]:
    """Fire a real completion at the backend/model. This IS the validation:
    confirms the URL, the key, and that the model name actually serves.
    Returns (ok, message)."""
    import httpx

    chat_url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": ENLISTMENT_PROMPT.format(model=model)}],
        "max_tokens": 64,
        "temperature": 0.7,
    }
    if engine == "ollama":
        payload["think"] = False
    try:
        with httpx.Client(timeout=40) as client:
            resp = client.post(chat_url, json=payload, headers=headers)
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "") or ""
            except Exception:
                detail = resp.text[:120]
            return False, f"HTTP {resp.status_code}{(' — ' + detail) if detail else ''}"
        data = resp.json()
        ch = (data.get("choices") or [{}])[0]
        msg = ch.get("message", {}) or {}
        # Reasoning models put text in reasoning/reasoning_content with empty content.
        reply = (msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
        reply = strip_thinking_tags(reply)
        if not reply:
            reply = "(model responded — empty visible text, likely a reasoning model)"
        return True, reply
    except Exception as e:
        return False, str(e)


def _configure_backend(n: int, detection=None) -> dict | None:
    """Walk the operator through ONE backend: locate → connect → pick model →
    VALIDATE (live completion) → name it. Returns a GRID_BACKENDS entry dict, or
    None if skipped. Sets nothing global."""
    from .detect_backends import check_backend_url, list_models_for_backend

    print()
    print(f"  ── Backend {n} " + "─" * 34)

    base_url = ""
    engine = "openai-compat"
    models: list[str] = []
    api_key = ""

    # Offer auto-detected backends only for the first one.
    if detection is not None and detection.found:
        print("  Detected on this host:")
        for i, be in enumerate(detection.backends, 1):
            tag = f" (v{be.version})" if be.version else ""
            print(f"    [{i}] {be.name} @ {be.url}{tag}")
        print(f"    [m] Enter a different URL (remote endpoint)")
        choice = input(f"\n  Use backend [1]: ").strip().lower()
        if choice in ("", *(str(i) for i in range(1, len(detection.backends) + 1))):
            b = detection.backends[(int(choice) - 1) if choice else 0]
            base_url = _norm_openai_base(b.url)
            engine = b.engine
            models = list(b.models or [])
            print(f"  → {b.name} @ {base_url}")

    # Manual URL (no detection, "different URL", or any extra backend).
    if not base_url:
        base_url = input("  Backend URL (OpenAI-compatible, e.g. https://host/v1): ").strip()
        if not base_url:
            return None
        base_url = _norm_openai_base(base_url)

        print("  Checking…", end=" ", flush=True)
        info = asyncio.run(check_backend_url(base_url))
        if info.get("auth_required"):
            print("auth required.")
            api_key = getpass.getpass("  API key for this backend: ").strip()
            info = asyncio.run(check_backend_url(base_url, api_key=api_key))
        if info.get("reachable"):
            print(f"connected ({info.get('name', 'OpenAI-compatible')}).")
        else:
            print("couldn't reach it.")
            if input("  Add it anyway? [y/N]: ").strip().lower() != "y":
                return None
        engine = info.get("engine") or "openai-compat"
        models = info.get("models") or asyncio.run(
            list_models_for_backend(base_url, engine, api_key=api_key)
        )

    backend_type = "ollama" if engine == "ollama" else "openai"

    # --- Model selection (from the real served list — no typos) ---
    print()
    if models:
        print("  Models served here:")
        for i, m in enumerate(models[:20], 1):
            print(f"    [{i}] {m}")
        if len(models) > 20:
            print(f"    … and {len(models) - 20} more")
        sel = input(f"  Select model [1] (or type a name): ").strip()
        if not sel:
            model = models[0]
        elif sel.isdigit() and 1 <= int(sel) <= len(models[:20]):
            model = models[int(sel) - 1]
        else:
            model = sel
    else:
        model = input("  Model name: ").strip()
    if not model:
        print("  No model — skipping this backend.")
        return None

    # --- Validate: a real completion against this exact (url, key, model) ---
    while True:
        print(f"  Validating {model}… ", end="", flush=True)
        ok, msg = _validate_backend(base_url, engine, model, api_key)
        if ok:
            wrapped = textwrap.fill(
                msg, width=64, initial_indent='✓\n      "', subsequent_indent="       "
            )
            print(wrapped + '"')
            break
        print("✗")
        print(f"      {msg}")
        nxt = input("  [r]etry  [s]kip backend  [c]ontinue anyway: ").strip().lower()
        if nxt == "r":
            continue
        if nxt == "c":
            print("  ⚠ added unvalidated — it won't serve jobs until it responds.")
            break
        return None

    # --- Name it on the grid ---
    suggested_grid = _slug(model)
    grid_model = input(f"  Name shown on the grid [{suggested_grid}]: ").strip() or suggested_grid
    conc = input("  Concurrency (parallel jobs) [1]: ").strip()
    concurrency = int(conc) if conc.isdigit() and int(conc) > 0 else 1

    print(f"  ✓ Backend {n}: {model} → \033[1m{grid_model}\033[0m (x{concurrency})")
    entry = {
        "type": backend_type,
        "url": base_url if backend_type == "ollama" else base_url.rstrip("/") + "/v1",
        "api_key": api_key,
        "model": model,
        "grid_model": grid_model,
        "concurrency": concurrency,
    }
    return entry


def quick_setup() -> dict:
    """Interactive terminal setup. Returns config dict ready for .env."""
    from .detect_backends import detect_backends

    print()
    print("  ┌─ Grid Inference Worker — quick setup ──────────┐")
    print("  │  Add one or more model backends; each is        │")
    print("  │  validated live before it goes on the grid.      │")
    print("  └─────────────────────────────────────────────────┘")

    print()
    print("  Scanning for local backends…", end=" ", flush=True)
    detection = detect_backends()
    print(f"found {len(detection.backends)}." if detection.found else "none found.")

    # --- Backends (one or many) ---
    backends: list[dict] = []
    first = _configure_backend(1, detection=detection)
    if not first:
        print("\n  No backend configured. Exiting.")
        sys.exit(1)
    backends.append(first)

    while True:
        print()
        if input("  Add another backend? [y/N]: ").strip().lower() != "y":
            break
        entry = _configure_backend(len(backends) + 1)
        if entry:
            backends.append(entry)

    # --- Grid API key (one account key powers all backends) ---
    print()
    print("  ── Grid account " + "─" * 33)
    api_key = getpass.getpass("  Grid API key (dashboard.aipowergrid.io): ").strip()
    if not api_key:
        print("  No API key provided. Exiting.")
        sys.exit(1)

    from .config import default_worker_name
    suggested = default_worker_name()
    worker_name = input(f"  Worker name [{suggested}]: ").strip() or suggested

    # --- Connection mode: streaming is the only live path (legacy /v2 retired) ---
    print()
    print("  Connection: ⚡ streaming (WebSocket) — recommended & default.")
    legacy = input("  Use legacy HTTP polling instead? [y/N]: ").strip().lower() == "y"
    streaming = not legacy

    # --- Assemble config ---
    first_b = backends[0]
    config = {
        "GRID_API_KEY": api_key,
        "GRID_WORKER_NAME": worker_name,
        "GRID_STREAMING": "true" if streaming else "false",
        "GRID_BACKENDS": json.dumps(backends),
        # Back-compat single-backend vars (also satisfy is_configured()).
        "BACKEND_TYPE": first_b["type"],
        "MODEL_NAME": first_b["model"],
        "GRID_MODEL_NAME": first_b["grid_model"],
    }
    if first_b["type"] == "ollama":
        config["OLLAMA_URL"] = first_b["url"]
    else:
        config["OPENAI_URL"] = first_b["url"]
    if first_b["api_key"]:
        config["OPENAI_API_KEY"] = first_b["api_key"]

    # --- Summary ---
    print()
    print("  ── Summary " + "─" * 38)
    print(f"  Worker:  {worker_name}   ({'streaming' if streaming else 'polling'})")
    for i, b in enumerate(backends, 1):
        print(f"    {i}. {b['model']:<28} → {b['grid_model']} (x{b['concurrency']})")
    print()

    write_env(config)
    print(f"  ✓ Saved {len(backends)} backend(s) to {ENV_PATH}")

    # --- Offer service installation ---
    print()
    print("  Install as a system service (start on boot, run in background)?")
    if input("  [Y/n]: ").strip().lower() != "n":
        print()
        service.install(verbose=True)
        config["_service_installed"] = True
    print()

    return config


def run(args):
    """Run worker in headless mode (no GUI, no web server)."""
    # Apply CLI flag overrides
    if args.api_key:
        Settings.GRID_API_KEY = args.api_key
    if args.model:
        Settings.MODEL_NAME = args.model
        if not Settings.GRID_MODEL_NAME:
            Settings.GRID_MODEL_NAME = f"grid/{args.model}"
    if args.backend_url:
        url = args.backend_url.rstrip("/")
        try:
            import httpx
            r = httpx.get(f"{url}/api/version", timeout=2)
            if r.status_code == 200:
                Settings.BACKEND_TYPE = "ollama"
                Settings.OLLAMA_URL = url
            else:
                raise Exception()
        except Exception:
            Settings.BACKEND_TYPE = "openai"
            Settings.OPENAI_URL = url + "/v1"
    if args.worker_name:
        Settings.GRID_WORKER_NAME = args.worker_name
    if getattr(args, "streaming", False):
        Settings.GRID_STREAMING = True

    if not is_configured():
        if args.no_setup:
            print("Error: GRID_API_KEY and MODEL_NAME are required.")
            print("Set them via env vars, .env, or CLI flags. Run without --no-setup for interactive setup.")
            sys.exit(1)
        config = quick_setup()
        reload_settings(config)

        if config.get("_service_installed"):
            return

    print("  Starting worker...")
    print()

    from .config import Settings
    if Settings.P2P_ENABLED:
        # P2P mode uses trio (not asyncio)
        from .p2p_client import run_p2p_worker
        print("  🔗 P2P mode — libp2p gossipsub connection")
        try:
            run_p2p_worker()
        except KeyboardInterrupt:
            print("\n  Shutting down...")
        return

    # Streaming-only: the multi-backend supervisor serves EVERY backend in
    # GRID_BACKENDS over WebSocket (/v1). The legacy /v2 HTTP poll loop is
    # retired, so GRID_STREAMING=false is refused rather than polling a dead
    # endpoint.
    if not Settings.GRID_STREAMING:
        print("  Error: legacy HTTP polling (/v2) is retired. This worker is")
        print("  streaming-only. Remove GRID_STREAMING=false from your .env")
        print("  (or set GRID_STREAMING=true) and restart.")
        sys.exit(1)

    from .ws_client import run_workers
    print("  ⚡ Streaming mode — WebSocket connection(s)")
    try:
        asyncio.run(run_workers())
    except KeyboardInterrupt:
        print("\n  Shutting down...")
