"""Headless mode — interactive quick setup + background worker loop."""

import asyncio
import getpass
import json
import sys
import textwrap
import time
import webbrowser

from .config import Settings
from .env_utils import ENV_PATH, is_configured, write_env, reload_settings
from .prompts import ENLISTMENT_PROMPT, strip_thinking_tags
from . import service


async def _authorize_grid_worker(worker_name: str) -> dict:
    """Complete Console device enrollment without exposing the generated key."""
    from .enrollment import EnrollmentClientError, poll_enrollment, start_enrollment

    try:
        enrollment = await start_enrollment(
            grid_api_url=Settings.GRID_API_URL,
            worker_name=worker_name,
        )
    except EnrollmentClientError as exc:
        raise RuntimeError(f"could not create worker approval: {exc}") from exc

    approval_url = enrollment["authorize_url"]
    print()
    print("  Open this secure Console approval link:")
    print(f"  {approval_url}")
    try:
        webbrowser.open(approval_url, new=2)
    except Exception:
        pass
    print("  Waiting for approval", end="", flush=True)

    expires_at = int(enrollment["expires_at"])
    delay = int(enrollment.get("poll_after_seconds", 2))
    while int(time.time()) < expires_at:
        await asyncio.sleep(delay)
        try:
            result = await poll_enrollment()
        except EnrollmentClientError as exc:
            raise RuntimeError(f"worker approval failed: {exc}") from exc
        print(".", end="", flush=True)
        if result.get("status") == "activated":
            print(" approved.")
            return result
    raise RuntimeError("worker approval expired; run setup again to create a new link")


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
        print("    [m] Enter a different URL (remote endpoint)")
        choice = input("\n  Use backend [1]: ").strip().lower()
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
        sel = input("  Select model [1] (or type a name): ").strip()
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

    from .config import default_worker_name
    suggested = default_worker_name()
    worker_name = input(f"  Worker name [{suggested}]: ").strip() or suggested

    # Console enrollment binds one credential to one exact connection name.
    # Multi-backend and parallel operators intentionally keep the advanced
    # account-key path until Core supports a safe set of worker identities.
    simple_worker = len(backends) == 1 and backends[0]["concurrency"] == 1
    print()
    print("  ── Grid account " + "─" * 33)
    credential_mode = "manual"
    api_key = ""
    if simple_worker:
        choice = input("  Connect securely through Console? [Y/m for manual key]: ").strip().lower()
        if choice != "m":
            try:
                asyncio.run(_authorize_grid_worker(worker_name))
                credential_mode = "console"
                backends[0]["name"] = worker_name
            except (RuntimeError, KeyboardInterrupt) as exc:
                print(f"\n  {exc}")
                print("  Falling back to advanced manual-key setup.")
    else:
        print("  Multiple backends or parallel slots require an advanced account API key.")
        print("  Worker-only Console enrollment currently binds one exact connection name.")

    if credential_mode == "manual":
        api_key = getpass.getpass(
            "  Existing Grid API key (console.aipowergrid.io/dashboard/api-key): "
        ).strip()
        if not api_key:
            print("  No API key provided. Exiting.")
            sys.exit(1)

    print()
    print("  Connection: streaming WebSocket")

    # --- Assemble config ---
    first_b = backends[0]
    config = {
        "GRID_WORKER_NAME": worker_name,
        "GRID_BACKENDS": json.dumps(backends),
        # Back-compat single-backend vars (also satisfy is_configured()).
        "BACKEND_TYPE": first_b["type"],
        "MODEL_NAME": first_b["model"],
        "GRID_MODEL_NAME": first_b["grid_model"],
    }
    if credential_mode == "manual":
        config["GRID_API_KEY"] = api_key
    else:
        config["GRID_ENROLLED_WORKER_NAME"] = worker_name
    if first_b["type"] == "ollama":
        config["OLLAMA_URL"] = first_b["url"]
    else:
        config["OPENAI_URL"] = first_b["url"]
    if first_b["api_key"]:
        config["OPENAI_API_KEY"] = first_b["api_key"]

    # --- Summary ---
    print()
    print("  ── Summary " + "─" * 38)
    credential_label = (
        "Console worker-only credential"
        if credential_mode == "console"
        else "advanced account key"
    )
    print(f"  Worker:  {worker_name}   (WebSocket, {credential_label})")
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

    if Settings.P2P_ENABLED:
        # P2P mode uses trio (not asyncio)
        from .p2p_client import run_p2p_worker
        print("  🔗 P2P mode — libp2p gossipsub connection")
        try:
            run_p2p_worker()
        except KeyboardInterrupt:
            print("\n  Shutting down...")
        return

    from .ws_client import run_workers
    print("  WebSocket connection(s)")
    try:
        asyncio.run(run_workers())
    except KeyboardInterrupt:
        print("\n  Shutting down...")
