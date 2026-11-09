# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hermetic regressions for slow backends and operator setup; not live Grid proof."""

import asyncio
import json
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from eth_account import Account

from inference_worker import enrollment, ws_client
from inference_worker.config import Settings, backend_read_timeout
from inference_worker.detect_backends import get_model_context_length
from inference_worker.env_utils import reload_settings
from inference_worker.web.routes import SettingsValidationError, _validated_backend_settings


def test_locked_security_dependencies_remain_patched():
    root = Path(__file__).resolve().parents[1]
    lock = tomllib.loads((root / "uv.lock").read_text())
    versions = {p["name"]: p["version"] for p in lock["package"]}
    assert versions["certifi"] == "2026.10.19"
    assert versions["pillow"] == "13.0.0"
    assert versions["setuptools"] == "84.1.0"


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(Settings, "validate", lambda: None)
    monkeypatch.setattr(ws_client, "get_signer", lambda: None)
    monkeypatch.setattr(Settings, "BACKEND_READ_TIMEOUT", 600)
    w = ws_client.StreamingWorker(spec=SimpleNamespace(
        name="test", model_name="test", grid_model_name="test",
        modalities=["text"], modalities_declared=True, backend_type="openai",
        url="http://127.0.0.1:8000/v1", api_key="", concurrency=1, schedule="",
    ))
    w.ws = AsyncMock()
    w.connected = True
    monkeypatch.setattr(w, "_backend_healthy", AsyncMock(return_value=True))
    monkeypatch.setattr(w, "_start_cancel_watcher", lambda *_: asyncio.create_task(asyncio.sleep(60)))
    return w


@pytest.mark.asyncio
async def test_generation_and_connection_timeouts_are_separate(worker):
    assert worker.backend.timeout.read == 600
    assert worker.backend.timeout.connect == 10
    assert worker.backend.timeout.write == 30
    assert worker.backend.timeout.pool == 10
    await worker.backend.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_format,stream", [
    ("openai-chat", True), ("openai-responses", True),
    ("anthropic", True), ("openai-responses", False), ("anthropic", False),
])
@pytest.mark.parametrize("healthy", [True, False])
async def test_timeout_is_reported_without_falsely_offlining_healthy_backend(
    worker, monkeypatch, caplog, api_format, stream, healthy,
):
    class TimeoutStream:
        async def __aenter__(self):
            raise httpx.ReadTimeout("private backend response must not be logged")

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(worker.backend, "stream", lambda *_args, **_kwargs: TimeoutStream())
    monkeypatch.setattr(worker.backend, "post", AsyncMock(side_effect=httpx.ReadTimeout("private")))
    worker._backend_healthy.return_value = healthy
    job = {"id": "job-timeout", "payload": {"api_format": api_format, "request": {
        "messages": [{"role": "user", "content": "private prompt"}], "stream": stream,
    }}}
    if healthy:
        await worker._handle_job(job)
        assert worker.connected
    else:
        with pytest.raises(ws_client.BackendUnavailable, match="after ReadTimeout"):
            await worker._handle_job(job)
    sent = [json.loads(c.args[0]) for c in worker.ws.send.await_args_list]
    assert len(sent) == 1
    assert sent[0]["type"] == "error"
    assert "ReadTimeout" in sent[0]["message"]
    assert sent[0]["client_error"] is False
    worker._backend_healthy.assert_awaited_once()
    assert "ReadTimeout" in caplog.text
    assert "private" not in caplog.text
    await worker.backend.aclose()


@pytest.mark.parametrize("value", ["", "0", "9", "3601", "inf", "nan", "600.5"])
def test_timeout_rejects_unbounded_or_invalid_settings(value):
    with pytest.raises(ValueError):
        backend_read_timeout(value)


@pytest.mark.parametrize("value", ["10", "600", "3600"])
def test_timeout_settings_reload_and_validation(monkeypatch, value):
    monkeypatch.setattr(Settings, "BACKEND_READ_TIMEOUT", 600)
    form = _validated_backend_settings({"GRID_BACKEND_READ_TIMEOUT": value})
    reload_settings(form)
    assert Settings.BACKEND_READ_TIMEOUT == int(value)


@pytest.mark.parametrize("value", ["", "0", "3601", "no", "600.5"])
def test_timeout_settings_api_rejects_invalid_values(value):
    with pytest.raises(SettingsValidationError):
        _validated_backend_settings({"GRID_BACKEND_READ_TIMEOUT": value})


@pytest.mark.asyncio
@pytest.mark.parametrize("models,expected", [
    ([{"name": "qwen3:8b", "context_length": 4096}], 4096),
    ([{"model": "qwen3:8b", "context_length": 32768}], 32768),
    ([{"name": "other", "context_length": 131072}], None),
    ([{"name": "qwen3:8b", "context_length": "40960"}], None),
    ([{"name": "qwen3:8b", "context_length": True}], None),
    ([{"name": "qwen3:8b", "context_length": 0}], None),
    ([{"name": "qwen3:8b"}], None),
    ([], None),
    ([None, {"name": "qwen3:8b", "context_length": 8192}], 8192),
    ([{"name": "qwen3:8b", "context_length": 4096},
      {"name": "qwen3:8b", "context_length": 32768}], 4096),
])
async def test_ollama_uses_loaded_context_not_architecture(models, expected):
    with respx.mock:
        route = respx.get("http://127.0.0.1:11434/api/ps").respond(200, json={"models": models})
        assert await get_model_context_length("http://127.0.0.1:11434", "ollama", "qwen3:8b") == {
            "context_length": expected,
        }
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_ollama_latest_alias_and_unknown_context_fallback(worker):
    worker.spec.backend_type = "ollama"
    worker.spec.url = "http://127.0.0.1:11434"
    worker.spec.model_name = "test"
    with respx.mock:
        route = respx.get("http://127.0.0.1:11434/api/ps").respond(200, json={"models": [
            {"name": "test:latest", "context_length": 16384},
        ]})
        assert await worker._detect_context() == 16384
        worker.spec.max_context = 8192
        assert await worker._detect_context() == 8192
        route.respond(404)
        assert await worker._detect_context() == 4096
    await worker.backend.aclose()


@pytest.mark.asyncio
async def test_expired_enrollment_allows_new_name_but_active_does_not(tmp_path, monkeypatch):
    signer = Account.create()
    monkeypatch.setattr(enrollment, "get_signer", lambda: signer)
    pending = tmp_path / "pending.json"
    with respx.mock:
        create = respx.post("https://api.aipowergrid.io/v1/workers/enrollments").respond(200, json={
            "enrollment_id": "enrollment_abcdefghijklmnopqrstuvwxyz",
            "authorize_url": "https://console.aipowergrid.io/dashboard/connect-worker/enrollment_abcdefghijklmnopqrstuvwxyz",
            "expires_at": int(time.time()) + 900,
            "poll_after_seconds": 2,
        })
        await enrollment.start_enrollment(grid_api_url="https://api.aipowergrid.io", worker_name="old", pending_path=pending)
        with pytest.raises(enrollment.EnrollmentClientError, match="another worker name"):
            await enrollment.start_enrollment(grid_api_url="https://api.aipowergrid.io", worker_name="new", pending_path=pending)
        assert create.call_count == 1
        state = json.loads(pending.read_text())
        state["expires_at"] = int(time.time()) - 1
        pending.write_text(json.dumps(state))
        result = await enrollment.start_enrollment(grid_api_url="https://api.aipowergrid.io", worker_name="new", pending_path=pending)
        assert result["worker_name"] == "new"
        assert create.call_count == 2
        assert json.loads(create.calls[-1].request.content)["worker_name"] == "new"
