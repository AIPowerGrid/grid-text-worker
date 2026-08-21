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

import ipaddress
import logging
import platform
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

# Shorter timeout + parallel probes so detection finishes in ~1–2s instead of 25s+
PROBE_TIMEOUT = 1.2

_PROHIBITED_METADATA_HOSTS = {
    "metadata",
    "metadata.google.internal",
}
_PROHIBITED_METADATA_ADDRESSES = {
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
    ipaddress.ip_address("169.254.169.254"),  # AWS, Azure, GCP, Oracle
    ipaddress.ip_address("169.254.170.2"),    # AWS ECS credentials
    ipaddress.ip_address("fd00:ec2::254"),    # AWS IMDS over IPv6
}


def validated_backend_url(value: str) -> str:
    """Validate an operator-supplied inference endpoint.

    Private and public backends are both supported intentionally.  The local
    dashboard may not target link-local metadata services, malformed URLs, or
    URLs with embedded credentials.  Hostnames are resolved once here so
    obviously unsafe address classes fail before any probe is attempted.
    """

    raw = str(value or "").strip()
    if not raw or len(raw) > 2048:
        raise ValueError("Backend URL is required and must be at most 2048 characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise ValueError("Backend URL must not contain control characters")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Backend URL must use http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Backend URL must contain a host and no embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Backend URL must not contain a query string or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Backend URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Backend URL contains an invalid port")

    host = parsed.hostname.rstrip(".").lower()
    if host in _PROHIBITED_METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        raise ValueError("Backend URL must not target a cloud metadata service")
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        addresses.add(ipaddress.ip_address(host))
    except ValueError:
        try:
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
                addresses.add(ipaddress.ip_address(item[4][0]))
        except socket.gaierror as exc:
            raise ValueError("Backend host could not be resolved") from exc
    if not addresses:
        raise ValueError("Backend host could not be resolved")
    if any(
        address in _PROHIBITED_METADATA_ADDRESSES
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        for address in addresses
    ):
        raise ValueError("Backend URL resolves to a prohibited address")

    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))

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
            backend.models = [m.get("name", "").removesuffix(":latest") for m in data.get("models", [])]
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

def _probe_one_engine(engine_def: dict) -> tuple[int, Optional[DetectedBackend]]:
    """Probe one engine (for parallel use); returns (port, backend or None). Each call uses its own client."""
    port = engine_def["default_port"]
    with httpx.Client(timeout=PROBE_TIMEOUT) as client:
        if port == 8000:
            backend = _identify_port_8000(client, f"http://127.0.0.1:{port}")
        else:
            backend = _probe_single_engine(client, engine_def)
    return (port, backend)


def detect_backends() -> DetectionResult:
    """Scan all known ports for running inference engines (parallel + short timeout)."""
    result = DetectionResult()

    # Ollama binary check (fast, keep in main thread)
    binary = shutil.which("ollama")
    if binary:
        result.ollama_binary = binary
        try:
            out = subprocess.run(
                ["ollama", "--version"], capture_output=True, text=True, timeout=2
            )
            if out.returncode == 0:
                result.ollama_version = out.stdout.strip()
        except Exception:
            pass

    # Probe all ports in parallel so total time ~ PROBE_TIMEOUT instead of N * timeout
    seen_ports: set[int] = set()
    with ThreadPoolExecutor(max_workers=len(KNOWN_ENGINES)) as executor:
        futures = {executor.submit(_probe_one_engine, eng): eng for eng in KNOWN_ENGINES}
        for future in as_completed(futures):
            port, backend = future.result()
            if port in seen_ports:
                continue
            if backend:
                result.backends.append(backend)
                seen_ports.add(port)

    return result


# Keep the old function name as an alias for backward compat in routes
def detect_ollama():
    """Backward-compatible wrapper — returns DetectionResult."""
    return detect_backends()


# ── URL-specific probing (used by setup wizard "Test" button) ────────────

async def check_backend_url(url: str, api_key: str = "") -> dict:
    """Probe a user-supplied URL and identify what engine is running.
    Returns dict with: reachable, engine, models, version, auth_required."""
    try:
        url = validated_backend_url(url)
    except ValueError:
        return {
            "reachable": False,
            "engine": None,
            "name": None,
            "models": [],
            "version": None,
            "auth_required": False,
            "error": "Invalid backend URL",
        }
    info = {"reachable": False, "engine": None, "name": None, "models": [], "version": None, "auth_required": False}

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=5, headers=headers) as client:
            # Try Ollama first
            try:
                resp = await client.get(f"{url}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    info["reachable"] = True
                    info["engine"] = "ollama"
                    info["name"] = "Ollama"
                    info["models"] = [m.get("name", "").removesuffix(":latest") for m in data.get("models", [])]
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
                if resp.status_code in (401, 403):
                    info["auth_required"] = True
                    return info
            except Exception:
                pass

            # Last resort: just try to connect
            if not info["reachable"]:
                try:
                    resp = await client.get(url)
                    if resp.status_code in (401, 403):
                        info["auth_required"] = True
                    elif resp.status_code < 500:
                        info["reachable"] = True
                        info["engine"] = "unknown"
                        info["name"] = "Unknown"
                except Exception:
                    pass

    except Exception:
        pass

    return info


async def list_models_for_backend(url: str, engine: str = None, api_key: str = "") -> list:
    """List models for any backend at the given URL."""
    try:
        url = validated_backend_url(url)
    except ValueError:
        return []
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            if engine == "ollama":
                resp = await client.get(f"{url}/api/tags")
                if resp.status_code == 200:
                    return [m.get("name", "").removesuffix(":latest") for m in resp.json().get("models", [])]
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


async def get_model_context_length(url: str, engine: str = None, model_name: str = None, api_key: str = "") -> dict:
    """Try to detect the model's context length from the backend.
    Returns {"context_length": int} or {"context_length": null}."""
    try:
        url = validated_backend_url(url)
    except ValueError:
        return {"context_length": None}
    ctx = None
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
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

            elif engine == "lmstudio":
                # LM Studio native API: GET /api/v1/models has max_context_length per model
                resp = await client.get(f"{url}/api/v1/models")
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("models", []):
                        key = m.get("key", "")
                        if model_name and key != model_name:
                            continue
                        # Prefer loaded instance's context_length, else model max
                        loaded = m.get("loaded_instances") or []
                        if loaded and "config" in loaded[0]:
                            ctx = loaded[0]["config"].get("context_length")
                        if ctx is None:
                            ctx = m.get("max_context_length")
                        if ctx is not None:
                            ctx = int(ctx)
                            break

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
        url = validated_backend_url(url)
    except ValueError:
        return {"ok": False, "error": "Invalid backend URL"}
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(
                f"{url}/api/pull",
                json={"name": model_name, "stream": False},
            )
            if resp.status_code == 200:
                return {"ok": True}
            return {"ok": False, "error": f"Backend returned HTTP {resp.status_code}"}
    except Exception:
        logger.exception("Ollama model pull failed")
        return {"ok": False, "error": "Model pull failed; see local worker logs"}
