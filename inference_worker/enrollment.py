# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Crash-resumable Console enrollment for a worker-only Grid credential."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from .config import CONFIG_DIR
from .env_utils import reload_settings, write_env
from .worker_identity import (
    DEFAULT_AUDIENCE,
    DEFAULT_CHAIN_ID,
    get_signer,
    install_delegation_certificate,
)

STATE_VERSION = 1
PROFILE_ID = "text-openai-compatible-v1"
DEFAULT_PENDING_PATH = CONFIG_DIR / "worker-enrollment-pending.json"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ENROLLMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")


class EnrollmentClientError(RuntimeError):
    """The worker could not safely complete Console enrollment."""


def grid_api_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EnrollmentClientError("Grid API URL must be HTTP(S)")
    if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise EnrollmentClientError("remote Grid enrollment requires HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EnrollmentClientError("Grid API URL must not contain credentials or query data")
    return value.rstrip("/")


async def start_enrollment(
    *,
    grid_api_url: str,
    worker_name: str,
    valid_days: int = 90,
    restart: bool = False,
    pending_path: str | Path = DEFAULT_PENDING_PATH,
    client: httpx.AsyncClient | None = None,
) -> Mapping[str, Any]:
    """Create or resume a short-lived device-style worker enrollment."""
    name = worker_name.strip()
    if not name or name != worker_name or len(name) > 120:
        raise EnrollmentClientError("worker name must be trimmed and at most 120 characters")
    base = grid_api_base_url(grid_api_url)
    pending = Path(pending_path).expanduser()
    if restart:
        pending.unlink(missing_ok=True)
    if pending.exists():
        state = _load_pending(pending)
        _require_pending_matches(state, grid_api_url=base, worker_name=name)
        if int(state["expires_at"]) > int(time.time()):
            return _public_state(state)
        pending.unlink()

    signer = get_signer()
    if signer is None:
        raise EnrollmentClientError("worker signing identity is unavailable")
    api_key = "grid_" + secrets.token_urlsafe(24)
    poll_token = secrets.token_urlsafe(32)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await _post_json(
            http,
            f"{base}/v1/workers/enrollments",
            {
                "worker_signer": signer.address.lower(),
                "worker_name": name,
                "profile_id": PROFILE_ID,
                "api_key": api_key,
                "poll_token_hash": hashlib.sha256(poll_token.encode("utf-8")).hexdigest(),
                "valid_days": valid_days,
            },
        )
    finally:
        if owns_client:
            await http.aclose()
    state = _validated_created_state(
        response,
        grid_api_url=base,
        worker_name=name,
        worker_signer=signer.address.lower(),
        api_key=api_key,
        poll_token=poll_token,
    )
    _atomic_private_json(pending, state)
    return _public_state(state)


async def poll_enrollment(
    *,
    pending_path: str | Path = DEFAULT_PENDING_PATH,
    client: httpx.AsyncClient | None = None,
) -> Mapping[str, Any]:
    """Poll once, installing and activating the credential when approved."""
    pending = Path(pending_path).expanduser()
    state = _load_pending(pending)
    if int(state["expires_at"]) <= int(time.time()):
        raise EnrollmentClientError("worker enrollment expired; start a new connection")
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        result: Mapping[str, Any]
        if state.get("certificate"):
            result = {"status": "complete", "certificate": state["certificate"]}
        else:
            result = await _post_json(
                http,
                (
                    f"{state['grid_api_url']}/v1/workers/enrollments/"
                    f"{state['enrollment_id']}/poll"
                ),
                {"poll_token": state["poll_token"]},
            )
        status = result.get("status")
        if status in {"pending", "prepared"}:
            return {**_public_state(state), "status": status}
        if status not in {"complete", "activated"}:
            raise EnrollmentClientError("Core returned an unknown worker enrollment state")
        certificate = result.get("certificate")
        if not certificate:
            raise EnrollmentClientError("Core completed enrollment without a certificate")
        install_delegation_certificate(
            certificate,
            worker_signer=state["worker_signer"],
            worker_name=state["worker_name"],
            chain_id=DEFAULT_CHAIN_ID,
            audience=DEFAULT_AUDIENCE,
        )
        if not state.get("certificate"):
            state = {**state, "certificate": certificate}
            _atomic_private_json(pending, state)
        values = {
            "GRID_API_KEY": state["api_key"],
            "GRID_ENROLLED_WORKER_NAME": state["worker_name"],
            "GRID_WORKER_NAME": state["worker_name"],
        }
        write_env(values)
        reload_settings(values)
        if status != "activated":
            activated = await _post_json(
                http,
                (
                    f"{state['grid_api_url']}/v1/workers/enrollments/"
                    f"{state['enrollment_id']}/ack"
                ),
                {"poll_token": state["poll_token"]},
            )
            if activated.get("status") != "activated":
                raise EnrollmentClientError("Core did not activate the worker credential")
        pending.unlink(missing_ok=True)
        return {
            "status": "activated",
            "worker_name": state["worker_name"],
            "payout_wallet": certificate["payload"]["payout_wallet"],
        }
    finally:
        if owns_client:
            await http.aclose()


def _validated_created_state(
    response: Mapping[str, Any],
    *,
    grid_api_url: str,
    worker_name: str,
    worker_signer: str,
    api_key: str,
    poll_token: str,
) -> dict[str, Any]:
    enrollment_id = response.get("enrollment_id")
    authorize_url = response.get("authorize_url")
    expires_at = response.get("expires_at")
    if not isinstance(enrollment_id, str) or not ENROLLMENT_ID_RE.fullmatch(enrollment_id):
        raise EnrollmentClientError("Core returned an invalid worker enrollment id")
    _validate_authorize_url(str(authorize_url or ""), grid_api_url, enrollment_id)
    if not isinstance(expires_at, int) or expires_at <= int(time.time()):
        raise EnrollmentClientError("Core returned an invalid worker enrollment expiry")
    return {
        "version": STATE_VERSION,
        "grid_api_url": grid_api_url,
        "worker_name": worker_name,
        "worker_signer": worker_signer,
        "api_key": api_key,
        "poll_token": poll_token,
        "enrollment_id": enrollment_id,
        "authorize_url": authorize_url,
        "expires_at": expires_at,
        "poll_after_seconds": max(1, min(int(response.get("poll_after_seconds", 2)), 10)),
    }


def _validate_authorize_url(value: str, grid_api_url: str, enrollment_id: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EnrollmentClientError("Core returned an invalid Console approval URL")
    api_host = (urlparse(grid_api_url).hostname or "").lower()
    if api_host == "api.aipowergrid.io":
        if parsed.scheme != "https" or parsed.hostname.lower() != "console.aipowergrid.io":
            raise EnrollmentClientError("Core returned an untrusted Console approval URL")
    elif parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise EnrollmentClientError("remote Console approval requires HTTPS")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/" + enrollment_id)
    ):
        raise EnrollmentClientError("Console approval URL is malformed")


def _public_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "pending",
        "enrollment_id": state["enrollment_id"],
        "authorize_url": state["authorize_url"],
        "expires_at": state["expires_at"],
        "poll_after_seconds": state["poll_after_seconds"],
        "worker_name": state["worker_name"],
    }


def _load_pending(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise EnrollmentClientError("worker enrollment state must not be a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrollmentClientError(f"cannot resume worker enrollment: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise EnrollmentClientError("pending worker enrollment is malformed")
    required = {
        "grid_api_url",
        "worker_name",
        "worker_signer",
        "api_key",
        "poll_token",
        "enrollment_id",
        "authorize_url",
        "expires_at",
        "poll_after_seconds",
    }
    if not required.issubset(value):
        raise EnrollmentClientError("pending worker enrollment is incomplete")
    grid_api_base_url(str(value["grid_api_url"]))
    if not ENROLLMENT_ID_RE.fullmatch(str(value["enrollment_id"])):
        raise EnrollmentClientError("pending worker enrollment id is invalid")
    _validate_authorize_url(
        str(value["authorize_url"]),
        str(value["grid_api_url"]),
        str(value["enrollment_id"]),
    )
    if not isinstance(value["expires_at"], int) or not isinstance(
        value["poll_after_seconds"], int
    ):
        raise EnrollmentClientError("pending worker enrollment timing is invalid")
    return value


def _require_pending_matches(state: Mapping[str, Any], **expected: str) -> None:
    for field, value in expected.items():
        if state.get(field) != value:
            raise EnrollmentClientError(
                f"pending enrollment targets another {field.replace('_', ' ')}; restart it"
            )


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        response = await client.post(url, json=dict(payload))
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EnrollmentClientError(f"worker enrollment request failed: {exc}") from exc
    if not response.is_success:
        detail = body.get("detail") if isinstance(body, Mapping) else None
        raise EnrollmentClientError(
            f"worker enrollment request failed ({response.status_code}): "
            f"{detail or 'Core rejected the request'}"
        )
    if not isinstance(body, Mapping):
        raise EnrollmentClientError("Core returned malformed worker enrollment JSON")
    return body


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise EnrollmentClientError("worker enrollment state must not be a symlink")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
