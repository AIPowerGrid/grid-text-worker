"""Inference backend detection, installation, and management utilities.

Scans known default ports to detect running inference engines:
  - Ollama       :11434   /api/tags, /api/version
  - vLLM         :8000    /v1/models  (server header or /version)
  - LM Studio    :1234    /v1/models
  - SGLang       :30000   /v1/models, /get_model_info
  - LMDeploy     :23333   /v1/models
  - TGI          :8080    /info
  - LocalAI      :8080    /v1/models  (has /models/available)
  - KoboldCpp    :5001    /api/v1/model
  - TabbyAPI     :5000    /v1/models, /v1/model
"""

import logging
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

# ── Known engines and their default ports / probe endpoints ──────────────

KNOWN_ENGINES = [
    {
        "name": "Ollama",
        "default_port": 11434,
        "probes": [
            {"path": "/api/tags", "id_field": "models", "engine": "ollama"},
        ],
        "version_path": "/api/version",
    },
    {
        "name": "vLLM",
        "default_port": 8000,
        "probes": [
            {"path": "/version", "id_field": "version", "engine": "vllm"},
            {"path": "/v1/models", "id_field": "data", "engine": "vllm"},
        ],
    },
    {
        "name": "LM Studio",
        "default_port": 1234,
        "probes": [
            {"path": "/v1/models", "id_field": "data", "engine": "lmstudio"},
        ],
    },
    {
        "name": "SGLang",
        "default_port": 30000,
        "probes": [
            {"path": "/get_model_info", "id_field": "model_path", "engine": "sglang"},
            {"path": "/v1/models", "id_field": "data", "engine": "sglang"},
        ],
    },
    {
        "name": "LMDeploy",
        "default_port": 23333,
        "probes": [
            {"path": "/v1/models", "id_field": "data", "engine": "lmdeploy"},
        ],
    },
    {
        "name": "TGI",
        "default_port": 8080,
        "probes": [
            {"path": "/info", "id_field": "model_id", "engine": "tgi"},
        ],
    },
    {
        "name": "KoboldCpp",
        "default_port": 5001,
        "probes": [
            {"path": "/api/v1/model", "id_field": "result", "engine": "koboldcpp"},
        ],
    },
    {
        "name": "TabbyAPI",
        "default_port": 5000,
        "probes": [
            {"path": "/v1/model", "id_field": None, "engine": "tabbyapi"},
            {"path": "/v1/models", "id_field": "data", "engine": "tabbyapi"},
        ],
    },
]


@dataclass
class DetectedBackend:
    """A single detected inference backend."""
    engine: str          # e.g. "ollama", "vllm", "lmstudio"
    name: str            # Human-readable, e.g. "Ollama", "vLLM"
    url: str             # e.g. "http://127.0.0.1:11434"
    models: List[str] = field(default_factory=list)
    version: Optional[str] = None
    api_type: str = "openai"  # "ollama" or "openai" (OpenAI-compatible)


@dataclass
class DetectionResult:
    """Result of a full backend scan."""
    backends: List[DetectedBackend] = field(default_factory=list)
    ollama_binary: Optional[str] = None
    ollama_version: Optional[str] = None

    @property
    def found(self) -> bool:
        return len(self.backends) > 0


# ── Probing helpers ──────────────────────────────────────────────────────

def _probe_url(client: httpx.Client, base_url: str, path: str) -> Optional[dict]:
    """Try GET on base_url+path, return parsed JSON or None."""
    try:
        resp = client.get(f"{base_url}{path}")
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _extract_models_openai(data: dict) -> List[str]:
    """Extract model IDs from an OpenAI /v1/models response."""
    models = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if mid:
            models.append(mid)
    return models


def _identify_engine_from_headers(resp_headers: dict) -> Optional[str]:
    """Try to identify engine from HTTP response headers."""
    server = (resp_headers.get("server") or "").lower()
    if "vllm" in server:
        return "vllm"
    if "uvicorn" in server:
        # Many engines use uvicorn, not definitive
        return None
    return None


def _probe_single_engine(client: httpx.Client, engine_def: dict) -> Optional[DetectedBackend]:
    """Probe a single engine definition on its default port."""
    port = engine_def["default_port"]
    base_url = f"http://127.0.0.1:{port}"

    for probe in engine_def["probes"]:
        data = _probe_url(client, base_url, probe["path"])
        if data is None:
            continue

        # We got a response — build the detection
        engine_id = probe["engine"]
        backend = DetectedBackend(
            engine=engine_id,
            name=engine_def["name"],
            url=base_url,
            api_type="ollama" if engine_id == "ollama" else "openai",
        )

        # Extract models based on engine type
        if engine_id == "ollama":
            backend.models = [m.get("name", "") for m in data.get("models", [])]
        elif engine_id == "tgi":
            model_id = data.get("model_id", "")
            if model_id:
                backend.models = [model_id]
            backend.version = data.get("version")
        elif engine_id == "koboldcpp":
            result = data.get("result", "")
            if result:
                backend.models = [result]
        elif engine_id == "sglang" and "model_path" in data:
            backend.models = [data["model_path"]]
        elif probe["id_field"] == "data":
            backend.models = _extract_models_openai(data)

        # Try to get version for engines that support it
        if engine_def.get("version_path"):
            ver_data = _probe_url(client, base_url, engine_def["version_path"])
            if ver_data and isinstance(ver_data, dict):
                backend.version = ver_data.get("version", str(ver_data))
        elif engine_id == "vllm" and probe["path"] == "/version":
            backend.version = data.get("version")

        return backend

    return None


# ── Additional identification for port 8000 (multiple engines share it) ──

def _identify_port_8000(client: httpx.Client, base_url: str) -> Optional[DetectedBackend]:
    """Port 8000 is shared by vLLM, LMDeploy, and potentially others.
    Try engine-specific endpoints to distinguish."""

    # Try vLLM /version first (unique to vLLM)
    data = _probe_url(client, base_url, "/version")
    if data and "version" in data:
        backend = DetectedBackend(
            engine="vllm", name="vLLM", url=base_url, api_type="openai",
            version=data.get("version"),
        )
        models_data = _probe_url(client, base_url, "/v1/models")
        if models_data:
            backend.models = _extract_models_openai(models_data)
        return backend

    # Try SGLang /get_model_info (if SGLang is on 8000 instead of 30000)
    data = _probe_url(client, base_url, "/get_model_info")
    if data and "model_path" in data:
        backend = DetectedBackend(
            engine="sglang", name="SGLang", url=base_url, api_type="openai",
        )
        backend.models = [data["model_path"]]
        return backend

    # Fall back to generic OpenAI-compatible check
    data = _probe_url(client, base_url, "/v1/models")
    if data:
        # Check response headers for clues
        try:
            resp = client.get(f"{base_url}/v1/models")
            engine_hint = _identify_engine_from_headers(dict(resp.headers))
        except Exception:
            engine_hint = None

        backend = DetectedBackend(
            engine=engine_hint or "openai-compat",
            name=engine_hint.upper() if engine_hint else "OpenAI-compatible",
            url=base_url,
            api_type="openai",
            models=_extract_models_openai(data),
        )
        return backend

    return None


# ── Main detection ───────────────────────────────────────────────────────

def detect_backends() -> DetectionResult:
    """Scan all known ports for running inference engines."""
    result = DetectionResult()

    # Check for Ollama binary
    binary = shutil.which("ollama")
    if binary:
        result.ollama_binary = binary
        try:
            out = subprocess.run(
                ["ollama", "--version"], capture_output=True, text=True, timeout=5
            )
            if out.returncode == 0:
                result.ollama_version = out.stdout.strip()
        except Exception:
            pass

    seen_ports = set()

    with httpx.Client(timeout=3) as client:
        for engine_def in KNOWN_ENGINES:
            port = engine_def["default_port"]
            if port in seen_ports:
                continue

            # Port 8000 needs special handling (shared by multiple engines)
            if port == 8000:
                backend = _identify_port_8000(client, f"http://127.0.0.1:{port}")
                if backend:
                    result.backends.append(backend)
                    seen_ports.add(port)
                continue

            backend = _probe_single_engine(client, engine_def)
            if backend:
                result.backends.append(backend)
                seen_ports.add(port)

    return result


# Keep the old function name as an alias for backward compat in routes
def detect_ollama():
    """Backward-compatible wrapper — returns DetectionResult."""
    return detect_backends()


# ── URL-specific probing (used by setup wizard "Test" button) ────────────

async def check_backend_url(url: str) -> dict:
    """Probe a user-supplied URL and identify what engine is running.
    Returns dict with: reachable, engine, models, version."""
    url = url.rstrip("/")
    info = {"reachable": False, "engine": None, "name": None, "models": [], "version": None}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            # Try Ollama first
            try:
                resp = await client.get(f"{url}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    info["reachable"] = True
                    info["engine"] = "ollama"
                    info["name"] = "Ollama"
                    info["models"] = [m.get("name", "") for m in data.get("models", [])]
                    try:
                        vr = await client.get(f"{url}/api/version")
                        if vr.status_code == 200:
                            info["version"] = vr.json().get("version")
                    except Exception:
                        pass
                    return info
            except Exception:
                pass

            # Try vLLM /version
            try:
                resp = await client.get(f"{url}/version")
                if resp.status_code == 200:
                    data = resp.json()
                    if "version" in data:
                        info["reachable"] = True
                        info["engine"] = "vllm"
                        info["name"] = "vLLM"
                        info["version"] = data.get("version")
            except Exception:
                pass

            # Try SGLang /get_model_info
            if not info["reachable"]:
                try:
                    resp = await client.get(f"{url}/get_model_info")
                    if resp.status_code == 200:
                        data = resp.json()
                        if "model_path" in data:
                            info["reachable"] = True
                            info["engine"] = "sglang"
                            info["name"] = "SGLang"
                            info["models"] = [data["model_path"]]
                            return info
                except Exception:
                    pass

            # Try TGI /info
            if not info["reachable"]:
                try:
                    resp = await client.get(f"{url}/info")
                    if resp.status_code == 200:
                        data = resp.json()
                        if "model_id" in data:
                            info["reachable"] = True
                            info["engine"] = "tgi"
                            info["name"] = "TGI"
                            info["models"] = [data["model_id"]]
                            info["version"] = data.get("version")
                            return info
                except Exception:
                    pass

            # Try KoboldCpp /api/v1/model
            if not info["reachable"]:
                try:
                    resp = await client.get(f"{url}/api/v1/model")
                    if resp.status_code == 200:
                        data = resp.json()
                        if "result" in data:
                            info["reachable"] = True
                            info["engine"] = "koboldcpp"
                            info["name"] = "KoboldCpp"
                            info["models"] = [data["result"]]
                            return info
                except Exception:
                    pass

            # Try generic OpenAI-compatible /v1/models
            try:
                resp = await client.get(f"{url}/v1/models")
                if resp.status_code == 200:
                    data = resp.json()
                    info["reachable"] = True
                    if not info["engine"]:
                        info["engine"] = "openai-compat"
                        info["name"] = "OpenAI-compatible"
                    info["models"] = _extract_models_openai(data)
                    return info
            except Exception:
                pass

            # Last resort: just try to connect
            if not info["reachable"]:
                try:
                    resp = await client.get(url)
                    if resp.status_code < 500:
                        info["reachable"] = True
                        info["engine"] = "unknown"
                        info["name"] = "Unknown"
                except Exception:
                    pass

    except Exception:
        pass

    return info


async def list_models_for_backend(url: str, engine: str = None) -> list:
    """List models for any backend at the given URL."""
    url = url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if engine == "ollama":
                resp = await client.get(f"{url}/api/tags")
                if resp.status_code == 200:
                    return [m.get("name", "") for m in resp.json().get("models", [])]
            elif engine == "koboldcpp":
                resp = await client.get(f"{url}/api/v1/model")
                if resp.status_code == 200:
                    result = resp.json().get("result", "")
                    return [result] if result else []
            elif engine == "tgi":
                resp = await client.get(f"{url}/info")
                if resp.status_code == 200:
                    mid = resp.json().get("model_id", "")
                    return [mid] if mid else []
            else:
                # OpenAI-compatible
                resp = await client.get(f"{url}/v1/models")
                if resp.status_code == 200:
                    return _extract_models_openai(resp.json())
    except Exception:
        pass
    return []


# ── Ollama-specific helpers ──────────────────────────────────────────────

def get_platform() -> str:
    """Return 'linux', 'macos', or 'windows'."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


def install_ollama() -> dict:
    """Install Ollama using the official install script (Linux/macOS only)."""
    plat = get_platform()
    if plat == "windows":
        return {
            "ok": False,
            "error": "Auto-install not supported on Windows. Download from https://ollama.com/download/windows",
        }
    try:
        proc = subprocess.run(
            ["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode == 0:
            return {"ok": True}
        return {"ok": False, "error": proc.stderr or "Install script failed"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Installation timed out (5 min)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def get_model_context_length(url: str, engine: str = None, model_name: str = None) -> dict:
    """Try to detect the model's context length from the backend.
    Returns {"context_length": int} or {"context_length": null}."""
    url = url.rstrip("/")
    ctx = None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if engine == "ollama" and model_name:
                # POST /api/show → model_info.<arch>.context_length
                resp = await client.post(f"{url}/api/show", json={"name": model_name})
                if resp.status_code == 200:
                    data = resp.json()
                    model_info = data.get("model_info", {})
                    for key, val in model_info.items():
                        if key.endswith(".context_length"):
                            ctx = int(val)
                            break

            elif engine == "tgi":
                resp = await client.get(f"{url}/info")
                if resp.status_code == 200:
                    data = resp.json()
                    ctx = data.get("max_total_tokens")

            elif engine == "sglang":
                resp = await client.get(f"{url}/get_model_info")
                if resp.status_code == 200:
                    data = resp.json()
                    ctx = data.get("context_length")

            elif engine == "koboldcpp":
                resp = await client.get(f"{url}/api/extra/true_max_context_length")
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, (int, float)):
                        ctx = int(data)
                    elif isinstance(data, dict):
                        ctx = data.get("value")

            else:
                # vLLM and other OpenAI-compat — check /v1/models for max_model_len
                resp = await client.get(f"{url}/v1/models")
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("data", []):
                        if model_name and m.get("id") != model_name:
                            continue
                        mml = m.get("max_model_len")
                        if mml:
                            ctx = int(mml)
                            break
    except Exception as e:
        logger.debug(f"Context length detection failed: {e}")

    return {"context_length": ctx}


async def pull_ollama_model(url: str, model_name: str) -> dict:
    """Pull a model in Ollama."""
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(
                f"{url}/api/pull",
                json={"name": model_name, "stream": False},
            )
            if resp.status_code == 200:
                return {"ok": True}
            return {"ok": False, "error": resp.text}
    except Exception as e:
        return {"ok": False, "error": str(e)}
