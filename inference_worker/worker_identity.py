# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Funds-less worker identity and payout-wallet delegation helpers."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from eth_account import Account
from eth_account.messages import encode_defunct

from .config import CONFIG_DIR

logger = logging.getLogger(__name__)

IDENTITY_VERSION = 1
DELEGATION_DOMAIN = "aipg-worker-delegation"
REGISTRATION_DOMAIN = "aipg-worker-registration"
DELEGATION_FIELDS = frozenset(
    {
        "version",
        "chain_id",
        "audience",
        "delegation_id",
        "payout_wallet",
        "worker_signer",
        "worker_name",
        "issued_at",
        "expires_at",
    }
)
DEFAULT_CHAIN_ID = 8453
DEFAULT_AUDIENCE = "api.aipowergrid.io"
DEFAULT_SIGNER_PATH = Path.home() / ".aipg" / "worker_signer.key"
DEFAULT_DELEGATION_PATH = CONFIG_DIR / "worker-delegation.json"

_SIGNER = None
_SIGNER_LOADED = False


class WorkerIdentityError(ValueError):
    """A local worker identity or delegation certificate is invalid."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def delegation_message(payload: Mapping[str, Any]) -> str:
    return f"{DELEGATION_DOMAIN}:v1:{canonical_json(payload)}"


def registration_message(payload: Mapping[str, Any]) -> str:
    return f"{REGISTRATION_DOMAIN}:v1:{canonical_json(payload)}"


def _load_or_create_signer():
    """Load or create the rig's funds-less secp256k1 signing identity."""
    try:
        configured = os.getenv("GRID_SIGNER_KEY", "").strip()
        if configured:
            return Account.from_key(configured)
        path = DEFAULT_SIGNER_PATH
        if path.exists():
            if path.is_symlink():
                raise WorkerIdentityError("worker signer path must not be a symlink")
            return Account.from_key(path.read_text(encoding="utf-8").strip())
        path.parent.mkdir(parents=True, exist_ok=True)
        account = Account.create()
        _atomic_private_text(path, account.key.hex())
        logger.info("generated worker signing identity %s at %s", account.address, path)
        return account
    except Exception as exc:
        logger.warning("signer init failed: %s", exc)
        return None


def get_signer():
    """Return one stable signing identity shared by every local connection."""
    global _SIGNER, _SIGNER_LOADED
    if not _SIGNER_LOADED:
        _SIGNER = _load_or_create_signer()
        _SIGNER_LOADED = True
    return _SIGNER


def validate_delegation_certificate(
    certificate: Any,
    *,
    worker_signer: str,
    worker_name: str,
    chain_id: int = DEFAULT_CHAIN_ID,
    audience: str = DEFAULT_AUDIENCE,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify the Console-signed payout delegation before trusting or storing it."""
    if not isinstance(certificate, Mapping) or set(certificate) != {"payload", "signature"}:
        raise WorkerIdentityError("worker delegation certificate is malformed")
    payload = certificate["payload"]
    signature = certificate["signature"]
    if not isinstance(payload, Mapping) or set(payload) != DELEGATION_FIELDS:
        raise WorkerIdentityError("worker delegation payload is malformed")
    if payload.get("version") != IDENTITY_VERSION:
        raise WorkerIdentityError("worker delegation version is unsupported")
    if payload.get("chain_id") != chain_id or payload.get("audience") != audience:
        raise WorkerIdentityError("worker delegation targets another Grid")
    if payload.get("worker_signer") != worker_signer.lower():
        raise WorkerIdentityError("worker delegation targets another signing key")
    if payload.get("worker_name") != worker_name:
        raise WorkerIdentityError("worker delegation targets another worker name")
    for field in ("payout_wallet", "worker_signer"):
        value = payload.get(field)
        if not isinstance(value, str) or not _is_address(value) or value != value.lower():
            raise WorkerIdentityError(f"worker delegation {field} is invalid")
    delegation_id = payload.get("delegation_id")
    if not isinstance(delegation_id, str) or not _is_hex(delegation_id, 32):
        raise WorkerIdentityError("worker delegation id is invalid")
    issued_at = _integer(payload.get("issued_at"), "issued_at")
    expires_at = _integer(payload.get("expires_at"), "expires_at")
    current = int(now if now is not None else time.time())
    if issued_at > current + 300 or expires_at <= current or expires_at <= issued_at:
        raise WorkerIdentityError("worker delegation is stale or expired")
    try:
        recovered = Account.recover_message(
            encode_defunct(text=delegation_message(payload)), signature=signature
        ).lower()
    except Exception as exc:
        raise WorkerIdentityError("worker delegation signature is invalid") from exc
    if recovered != payload["payout_wallet"]:
        raise WorkerIdentityError("worker delegation was not signed by its payout wallet")
    return {"payload": dict(payload), "signature": str(signature)}


def install_delegation_certificate(
    certificate: Any,
    *,
    worker_signer: str,
    worker_name: str,
    path: str | Path = DEFAULT_DELEGATION_PATH,
    chain_id: int = DEFAULT_CHAIN_ID,
    audience: str = DEFAULT_AUDIENCE,
) -> dict[str, Any]:
    verified = validate_delegation_certificate(
        certificate,
        worker_signer=worker_signer,
        worker_name=worker_name,
        chain_id=chain_id,
        audience=audience,
    )
    _atomic_private_json(Path(path).expanduser(), verified)
    return verified


def load_delegation_certificate(
    path: str | Path = DEFAULT_DELEGATION_PATH,
) -> dict[str, Any]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise WorkerIdentityError("worker delegation path must not be a symlink")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerIdentityError(f"cannot read worker delegation: {exc}") from exc
    return value


def build_registration_proof(
    *,
    worker_name: str,
    models: Sequence[str],
    job_types: Sequence[str],
    bridge_agent: str,
    delegation_path: str | Path = DEFAULT_DELEGATION_PATH,
) -> dict[str, Any] | None:
    """Sign a fresh registration bound to the advertised worker capabilities."""
    source = Path(delegation_path).expanduser()
    if not source.exists():
        return None
    signer = get_signer()
    if signer is None:
        raise WorkerIdentityError("worker signer is unavailable")
    certificate = validate_delegation_certificate(
        load_delegation_certificate(source),
        worker_signer=signer.address.lower(),
        worker_name=worker_name,
    )
    payload = {
        "version": IDENTITY_VERSION,
        "timestamp": int(time.time()),
        "nonce": secrets.token_hex(16),
        "worker_signer": signer.address.lower(),
        "worker_name": worker_name,
        "models": list(models),
        "job_types": list(job_types),
        "bridge_agent": bridge_agent,
        "profile_digest": None,
        "profile_recipe_root": None,
    }
    signature = signer.sign_message(
        encode_defunct(text=registration_message(payload))
    ).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    return {"payload": payload, "signature": signature, "delegation": certificate}


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_private_text(path, json.dumps(value, sort_keys=True, separators=(",", ":")))


def _atomic_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise WorkerIdentityError(f"private worker path must not be a symlink: {path}")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _is_address(value: str) -> bool:
    return len(value) == 42 and value.startswith("0x") and _is_hex(value[2:], 40)


def _is_hex(value: str, length: int) -> bool:
    if len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkerIdentityError(f"worker delegation {label} is invalid")
    return value
