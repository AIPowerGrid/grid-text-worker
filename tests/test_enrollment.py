# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
import os
import time
from types import SimpleNamespace

import httpx
import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_defunct

from inference_worker.enrollment import (
    EnrollmentClientError,
    poll_enrollment,
    start_enrollment,
)
from inference_worker.headless import _authorize_grid_worker
from inference_worker import headless
from inference_worker.worker_identity import (
    WorkerIdentityError,
    build_registration_proof,
    delegation_message,
    install_delegation_certificate,
)


def _certificate(worker, wallet, worker_name="Text-Inference-Worker-test"):
    issued = int(time.time())
    payload = {
        "version": 1,
        "chain_id": 8453,
        "audience": "api.aipowergrid.io",
        "delegation_id": "ab" * 16,
        "payout_wallet": wallet.address.lower(),
        "worker_signer": worker.address.lower(),
        "worker_name": worker_name,
        "issued_at": issued,
        "expires_at": issued + 90 * 86400,
    }
    signature = wallet.sign_message(
        encode_defunct(text=delegation_message(payload))
    ).signature.hex()
    return {"payload": payload, "signature": signature}


@pytest.mark.asyncio
@respx.mock
async def test_console_enrollment_installs_worker_only_key_without_returning_it(
    tmp_path, monkeypatch
):
    worker = Account.create()
    wallet = Account.create()
    pending = tmp_path / "pending.json"
    captured = {}

    monkeypatch.setattr("inference_worker.enrollment.get_signer", lambda: worker)
    monkeypatch.setattr(
        "inference_worker.enrollment.install_delegation_certificate",
        lambda certificate, **kwargs: captured.setdefault("certificate", certificate),
    )
    monkeypatch.setattr(
        "inference_worker.enrollment.write_env",
        lambda values: captured.setdefault("write_env", values),
    )
    monkeypatch.setattr(
        "inference_worker.enrollment.reload_settings",
        lambda values: captured.setdefault("reload_settings", values),
    )

    def create_response(request):
        body = json.loads(request.content)
        captured["create"] = body
        return httpx.Response(
            200,
            json={
                "enrollment_id": "enrollment_abcdefghijklmnopqrstuvwxyz",
                "authorize_url": (
                    "https://console.aipowergrid.io/dashboard/connect-worker/"
                    "enrollment_abcdefghijklmnopqrstuvwxyz"
                ),
                "expires_at": int(time.time()) + 900,
                "poll_after_seconds": 2,
            },
        )

    def poll_response(request):
        body = json.loads(request.content)
        captured["poll"] = body
        assert hashlib.sha256(body["poll_token"].encode()).hexdigest() == captured[
            "create"
        ]["poll_token_hash"]
        return httpx.Response(200, json={"status": "complete", "certificate": _certificate(worker, wallet)})

    def ack_response(request):
        captured["ack"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "activated"})

    respx.post("https://api.aipowergrid.io/v1/workers/enrollments").mock(
        side_effect=create_response
    )
    respx.post(
        "https://api.aipowergrid.io/v1/workers/enrollments/"
        "enrollment_abcdefghijklmnopqrstuvwxyz/poll"
    ).mock(side_effect=poll_response)
    respx.post(
        "https://api.aipowergrid.io/v1/workers/enrollments/"
        "enrollment_abcdefghijklmnopqrstuvwxyz/ack"
    ).mock(side_effect=ack_response)

    started = await start_enrollment(
        grid_api_url="https://api.aipowergrid.io",
        worker_name="Text-Inference-Worker-test",
        pending_path=pending,
    )
    assert "api_key" not in started
    assert started["status"] == "pending"
    assert pending.exists()
    if os.name != "nt":
        assert pending.stat().st_mode & 0o777 == 0o600

    completed = await poll_enrollment(pending_path=pending)
    assert completed == {
        "status": "activated",
        "worker_name": "Text-Inference-Worker-test",
        "payout_wallet": wallet.address.lower(),
    }
    assert not pending.exists()
    assert captured["write_env"] == captured["reload_settings"]
    assert captured["write_env"]["GRID_API_KEY"] == captured["create"]["api_key"]
    assert captured["write_env"]["GRID_ENROLLED_WORKER_NAME"] == "Text-Inference-Worker-test"
    assert captured["ack"] == captured["poll"]


@pytest.mark.asyncio
@respx.mock
async def test_production_api_rejects_untrusted_console_url(tmp_path, monkeypatch):
    monkeypatch.setattr("inference_worker.enrollment.get_signer", Account.create)
    respx.post("https://api.aipowergrid.io/v1/workers/enrollments").mock(
        return_value=httpx.Response(
            200,
            json={
                "enrollment_id": "enrollment_abcdefghijklmnopqrstuvwxyz",
                "authorize_url": "https://attacker.example/steal",
                "expires_at": int(time.time()) + 900,
            },
        )
    )
    with pytest.raises(EnrollmentClientError, match="untrusted Console"):
        await start_enrollment(
            grid_api_url="https://api.aipowergrid.io",
            worker_name="Text-Inference-Worker-test",
            pending_path=tmp_path / "pending.json",
        )


def test_registration_proof_is_bound_to_worker_capabilities(tmp_path, monkeypatch):
    worker = Account.create()
    wallet = Account.create()
    path = tmp_path / "delegation.json"
    install_delegation_certificate(
        _certificate(worker, wallet),
        worker_signer=worker.address.lower(),
        worker_name="Text-Inference-Worker-test",
        path=path,
    )
    monkeypatch.setattr("inference_worker.worker_identity.get_signer", lambda: worker)

    proof = build_registration_proof(
        worker_name="Text-Inference-Worker-test",
        models=["gpt-oss-20b"],
        job_types=["text"],
        bridge_agent="Grid Inference Worker:test",
        delegation_path=path,
    )
    assert proof["payload"]["models"] == ["gpt-oss-20b"]
    assert proof["payload"]["job_types"] == ["text"]
    assert len(proof["payload"]["nonce"]) == 32
    recovered = Account.recover_message(
        encode_defunct(
            text=(
                "aipg-worker-registration:v1:"
                + json.dumps(
                    proof["payload"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
            )
        ),
        signature=proof["signature"],
    )
    assert recovered.lower() == worker.address.lower()

    with pytest.raises(WorkerIdentityError, match="another worker name"):
        build_registration_proof(
            worker_name="different-worker",
            models=["gpt-oss-20b"],
            job_types=["text"],
            bridge_agent="Grid Inference Worker:test",
            delegation_path=path,
        )


@pytest.mark.asyncio
async def test_headless_console_enrollment_opens_approval_and_returns_no_key(
    monkeypatch, capsys
):
    opened = []

    async def fake_start(**kwargs):
        assert kwargs["worker_name"] == "Text-Inference-Worker-test"
        return {
            "status": "pending",
            "authorize_url": (
                "https://console.aipowergrid.io/dashboard/connect-worker/"
                "enrollment_abcdefghijklmnopqrstuvwxyz"
            ),
            "expires_at": int(time.time()) + 60,
            "poll_after_seconds": 1,
        }

    async def fake_poll():
        return {
            "status": "activated",
            "worker_name": "Text-Inference-Worker-test",
            "payout_wallet": Account.create().address.lower(),
        }

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("inference_worker.enrollment.start_enrollment", fake_start)
    monkeypatch.setattr("inference_worker.enrollment.poll_enrollment", fake_poll)
    monkeypatch.setattr("inference_worker.headless.asyncio.sleep", no_wait)
    monkeypatch.setattr(
        "inference_worker.headless.webbrowser.open",
        lambda url, new=0: opened.append((url, new)),
    )

    result = await _authorize_grid_worker("Text-Inference-Worker-test")
    assert result["status"] == "activated"
    assert "api_key" not in result
    assert opened == [
        (
            "https://console.aipowergrid.io/dashboard/connect-worker/"
            "enrollment_abcdefghijklmnopqrstuvwxyz",
            2,
        )
    ]
    output = capsys.readouterr().out
    assert "approved" in output
    assert "grid_" not in output


def test_headless_run_uses_module_settings_without_local_import_shadowing(
    monkeypatch, capsys
):
    args = SimpleNamespace(
        api_key=None,
        model=None,
        backend_url=None,
        worker_name=None,
        no_setup=True,
    )

    async def stop_immediately():
        raise KeyboardInterrupt

    monkeypatch.setattr(headless, "is_configured", lambda: True)
    monkeypatch.setattr("inference_worker.ws_client.run_workers", stop_immediately)
    monkeypatch.setattr(headless.Settings, "P2P_ENABLED", False)

    headless.run(args)

    assert "Starting worker" in capsys.readouterr().out
