import os
import re
import socket
import sys
import uuid
from pathlib import Path
from dotenv import load_dotenv


def _config_dir() -> Path:
    """Stable config directory that persists across binary updates."""
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        d = base / "grid-inference-worker"
        d.mkdir(parents=True, exist_ok=True)
        # Migrate from old location (next to exe)
        env_new = d / ".env"
        if not env_new.exists():
            env_old = Path(sys.executable).resolve().parent / ".env"
            if env_old.exists():
                import shutil
                shutil.copy2(env_old, env_new)
        return d
    return Path.cwd()


CONFIG_DIR = _config_dir()
ENV_FILE = CONFIG_DIR / ".env"

load_dotenv(ENV_FILE)


def default_worker_name() -> str:
    """A worker name that's stable per-machine but unique across machines.

    The grid keys a worker by its name, so every node defaulting to the same
    "Text-Inference-Worker" collides — they fight over one identity and stats
    are meaningless. Derive a stable suffix from the hostname; for generic/empty
    hostnames fall back to a random id persisted in the config dir so it stays
    constant across restarts (a name that changes every boot fragments the
    worker's reputation and uptime).
    """
    host = socket.gethostname() or ""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", host).strip("-")
    if slug and slug.lower() not in ("localhost", "localhost-localdomain"):
        return f"Text-Inference-Worker-{slug[:24]}"
    idfile = CONFIG_DIR / ".worker_id"
    try:
        sid = idfile.read_text().strip() if idfile.exists() else ""
        if not sid:
            sid = uuid.uuid4().hex[:8]
            idfile.write_text(sid)
    except OSError:
        sid = uuid.uuid4().hex[:8]
    return f"Text-Inference-Worker-{sid}"


class Settings:
    GRID_API_KEY = os.getenv("GRID_API_KEY", "")
    # `or` (not getenv default) so an explicitly-empty env var still gets a name.
    GRID_WORKER_NAME = os.getenv("GRID_WORKER_NAME") or default_worker_name()
    GRID_API_URL = os.getenv("GRID_API_URL", "https://api.aipowergrid.io")
    NSFW = os.getenv("GRID_NSFW", "true").lower() == "true"
    # Concurrency = number of parallel grid connections this box serves (each is
    # one in-flight job). Set below your backend's comfortable batch size to
    # leave headroom for any local app sharing the same vLLM. Default 1 (serial).
    MAX_THREADS = int(os.getenv("GRID_MAX_THREADS", "1"))
    # Optional time-based throttle. JSON list of windows (operator local time):
    #   [{"days":"mon-fri","start":"08:00","end":"18:00","concurrency":1},
    #    {"days":"sat-sun","concurrency":5}]
    # Outside any window → MAX_THREADS. concurrency:0 = pause (off the grid).
    GRID_SCHEDULE = os.getenv("GRID_SCHEDULE", "")
    # Generous defaults for 2026-era models. MAX_LENGTH is the OUTPUT budget used
    # only when a request doesn't specify max_tokens — an explicit client value is
    # honored as-is (we no longer clamp it down). 4096 used to guillotine long
    # generations and starve multi-step tool calls.
    MAX_LENGTH = int(os.getenv("GRID_MAX_LENGTH", "32768"))
    MAX_CONTEXT_LENGTH = int(os.getenv("GRID_MAX_CONTEXT_LENGTH", "131072"))

    # Backend type: "ollama" (easy mode) or "openai" (advanced/custom)
    BACKEND_TYPE = os.getenv("BACKEND_TYPE", "ollama")

    # Ollama settings
    OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")

    # OpenAI-compatible settings (for vllm, sglang, lmdeploy, etc.)
    OPENAI_URL = os.getenv("OPENAI_URL", "http://127.0.0.1:8000/v1")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    # Minimize or disable reasoning on backends that support it (e.g. "low", "none"). Leave unset to not send.
    REASONING_EFFORT = os.getenv("REASONING_EFFORT", "").lower() or None

    # Model to serve (e.g. "llama3.2:3b" for ollama, "meta-llama/..." for openai)
    MODEL_NAME = os.getenv("MODEL_NAME", "")

    # Grid model name (what to advertise to the grid, with domain prefix)
    GRID_MODEL_NAME = os.getenv("GRID_MODEL_NAME", "")

    # Streaming (WebSocket /v1) is the only live mode — the legacy /v2 poll
    # queue is retired. Defaults ON; set "false" only knowingly (the worker
    # refuses rather than poll the dead /v2 endpoint).
    GRID_STREAMING = os.getenv("GRID_STREAMING", "true").lower() == "true"
    GRID_STREAMING_URL = os.getenv("GRID_STREAMING_URL", "")  # Override WS URL (auto-derived from GRID_API_URL if empty)

    # P2P mode — connect via libp2p gossipsub instead of WebSocket
    P2P_ENABLED = os.getenv("P2P_ENABLED", "false").lower() == "true"
    P2P_LISTEN_PORT = int(os.getenv("P2P_LISTEN_PORT", "4001"))
    P2P_BOOTSTRAP_PEERS: list[str] = [
        p.strip()
        for p in os.getenv("P2P_BOOTSTRAP_PEERS", "").split(",")
        if p.strip()
    ]

    # Wallet address for rewards (Base chain)
    WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

    # Dashboard auth token (auto-generated on first run)
    DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")

    @classmethod
    def validate(cls):
        if not cls.GRID_API_KEY:
            raise RuntimeError("GRID_API_KEY environment variable is required.")


# ── Multi-backend support ─────────────────────────────────────────────────
# One worker binary can serve SEVERAL (backend, model) pairs at once — e.g. a
# local vLLM plus a remote endpoint — each advertised to the grid as its own
# model with its own concurrency. Configure via GRID_BACKENDS (JSON list); if
# unset, a single backend is built from the classic env vars above (so existing
# single-model deployments are unchanged).
from dataclasses import dataclass, field  # noqa: E402


@dataclass
class Backend:
    name: str             # connection-name base (unique per backend on this host)
    backend_type: str     # "openai" | "ollama"
    url: str              # openai base (…/v1) or ollama base
    api_key: str
    model_name: str       # model id the backend expects
    grid_model_name: str  # name advertised to the grid
    concurrency: int = 1
    schedule: str = ""    # optional per-backend GRID_SCHEDULE JSON
    modalities: list[str] = field(default_factory=lambda: ["text"])  # input modalities advertised to the grid


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:24] or "model"


def _parse_modalities(s: dict) -> list[str]:
    """Input modalities this backend's model accepts, DECLARED explicitly.

    Auto-detection isn't reliable: vLLM exposes no modality field on /v1/models
    and silently ignores image parts on a text model (returns 200), so probing
    gives false positives. The operator knows their model, so they declare it:
    `"modalities": ["text","image"]` or the shorthand `"vision": true`. Text is
    always included. (A validator can later VERIFY this claim and revoke a lie.)
    """
    mods = s.get("modalities")
    if isinstance(mods, list) and mods:
        out = [m for m in mods if m in ("text", "image", "video")]
    elif s.get("vision"):
        out = ["text", "image"]
    else:
        out = ["text"]
    if "text" not in out:
        out = ["text"] + out
    return out


def load_backends() -> list[Backend]:
    """Return the list of backends this worker serves.

    GRID_BACKENDS = JSON array of objects:
      [{"type":"openai","url":"https://host/v1","api_key":"sk-…",
        "model":"Qwen3.6-27B-FP8","grid_model":"qwen3-27b","concurrency":1},
       {"type":"openai","url":"http://10.0.0.5:8000/v1","model":"gpt-oss-120b",
        "grid_model":"gpt-oss-120b","concurrency":2}]
    Fields: type(default openai), url, api_key, model, grid_model(default
    grid/<model>), concurrency(default 1), schedule, name(default <worker>-<slug>).
    """
    raw = os.getenv("GRID_BACKENDS", "").strip()
    if raw:
        import json
        out: list[Backend] = []
        for s in json.loads(raw):
            bt = (s.get("type") or "openai").lower()
            model = s.get("model") or s.get("model_name") or ""
            grid_model = s.get("grid_model") or s.get("grid_model_name") or f"grid/{model}"
            default_url = Settings.OLLAMA_URL if bt == "ollama" else Settings.OPENAI_URL
            out.append(Backend(
                name=s.get("name") or f"{Settings.GRID_WORKER_NAME}-{_slug(grid_model)}",
                backend_type=bt,
                url=s.get("url") or default_url,
                api_key=s.get("api_key", ""),
                model_name=model,
                grid_model_name=grid_model,
                concurrency=int(s.get("concurrency", 1)),
                schedule=s.get("schedule", ""),
                modalities=_parse_modalities(s),
            ))
        if out:
            return out
    # Back-compat: a single backend from the classic env vars.
    return [Backend(
        name=Settings.GRID_WORKER_NAME,
        backend_type=Settings.BACKEND_TYPE,
        url=(Settings.OLLAMA_URL if Settings.BACKEND_TYPE == "ollama" else Settings.OPENAI_URL),
        api_key=Settings.OPENAI_API_KEY,
        model_name=Settings.MODEL_NAME,
        grid_model_name=Settings.GRID_MODEL_NAME or f"grid/{Settings.MODEL_NAME}",
        concurrency=Settings.MAX_THREADS,
        schedule=Settings.GRID_SCHEDULE,
    )]
