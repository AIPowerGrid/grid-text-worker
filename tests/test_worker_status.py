# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local handshake/lifecycle tests; these are not a live Grid job proof."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from inference_worker import ws_client
from inference_worker.config import Settings
from inference_worker.web.app import worker_state
from inference_worker.web.routes import _aggregate_session_stats, api_status


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(Settings, "validate", lambda: None)
    monkeypatch.setattr(ws_client, "get_signer", lambda: None)
    monkeypatch.setattr(ws_client.httpx, "AsyncClient", lambda **kwargs: AsyncMock())
    spec = SimpleNamespace(
        name="test-worker",
        model_name="test-model",
        grid_model_name="grid/test-model",
        modalities=["text"],
        modalities_declared=True,
        backend_type="openai",
        url="http://127.0.0.1:8000/v1",
        api_key="",
        concurrency=1,
        schedule="",
    )
    w = ws_client.StreamingWorker(spec=spec)
    monkeypatch.setattr(w, "_backend_healthy", AsyncMock(return_value=True))
    monkeypatch.setattr(w, "_probe_formats", AsyncMock(return_value=["openai-chat"]))
    monkeypatch.setattr(w, "_detect_context", AsyncMock(return_value=4096))
    return w


@pytest.mark.asyncio
async def test_only_ready_handshake_marks_connected(worker, monkeypatch):
    ws = AsyncMock()
    ws.recv.return_value = json.dumps({"type": "ready", "worker_id": "worker-test"})
    monkeypatch.setattr(ws_client.websockets, "connect", AsyncMock(return_value=ws))
    assert not worker.connected
    await worker.connect()
    assert worker.connected
    assert worker.worker_id == "worker-test"
    assert worker.connection_error is None
    await worker.close()
    assert not worker.connected
    assert worker.worker_id is None
    ws.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"type": "error", "message": "a remote body must not reach status"},
        {"type": "ready"},
        {"type": "ready", "worker_id": ""},
        {"type": "ready", "worker_id": 123},
        {"type": "other"},
    ],
)
async def test_invalid_registration_never_marks_connected(
    worker, monkeypatch, response
):
    ws = AsyncMock()
    ws.recv.return_value = json.dumps(response)
    monkeypatch.setattr(ws_client.websockets, "connect", AsyncMock(return_value=ws))
    with pytest.raises(ConnectionError):
        await worker.connect()
    assert not worker.connected
    assert worker.worker_id is None
    assert worker.connection_error
    assert "remote body" not in worker.connection_error
    await worker.close()


@pytest.mark.asyncio
async def test_disconnect_clears_status_before_backoff(worker, monkeypatch):
    backoff = asyncio.Event()
    ws = AsyncMock()

    async def connect():
        worker.connected = True
        worker.worker_id = "worker-test"
        worker.ws = ws

    async def sleep(_delay):
        backoff.set()
        await asyncio.Future()

    monkeypatch.setattr(worker, "connect", connect)
    monkeypatch.setattr(
        worker, "_message_loop", AsyncMock(side_effect=ConnectionError())
    )
    monkeypatch.setattr(ws_client.asyncio, "sleep", sleep)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(backoff.wait(), 1)
        assert not worker.connected
        assert worker.worker_id is None
        assert worker.connection_error
        ws.close.assert_awaited_once()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_supervisor_waits_for_connections_to_close(worker, monkeypatch):
    started = asyncio.Event()

    async def run():
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(worker, "run", run)
    monkeypatch.setattr(ws_client, "StreamingWorker", lambda **kwargs: worker)
    monkeypatch.setattr("inference_worker.config.load_backends", lambda: [worker.spec])
    registry = {}
    expected = set()
    task = asyncio.create_task(
        ws_client.run_workers(active_workers=registry, expected_workers=expected)
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert registry == {"test-worker": worker}
        assert expected == {"test-worker"}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert registry == {}
    assert expected == set()
    worker.backend.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("running", "connections", "expected"),
    [
        (False, [True], False),
        (True, [], False),
        (True, [False], False),
        (True, [True], True),
        (True, [True, False], False),
        (True, [True, True], True),
    ],
)
async def test_status_requires_all_active_connections(
    monkeypatch, running, connections, expected
):
    workers = {
        str(i): SimpleNamespace(connected=connected, connection_error=None)
        for i, connected in enumerate(connections)
    }
    monkeypatch.setitem(worker_state, "running", running)
    monkeypatch.setitem(worker_state, "workers", workers)
    monkeypatch.setitem(worker_state, "expected_workers", set(workers))
    result = await api_status()
    assert result["grid_connected"] is expected
    assert result["connected_workers"] == (sum(connections) if running else 0)
    assert result["total_workers"] == (len(connections) if running else 0)


@pytest.mark.asyncio
async def test_status_exposes_connection_error_without_credentials(worker, monkeypatch):
    worker.connection_error = (
        "Grid rejected registration; check the API key and worker name."
    )
    monkeypatch.setitem(worker_state, "running", True)
    monkeypatch.setitem(worker_state, "workers", {worker.name: worker})
    monkeypatch.setitem(worker_state, "expected_workers", {worker.name})
    monkeypatch.setattr(
        Settings, "GRID_API_KEY", "not-a-real-key-never-return-in-status"
    )
    result = await api_status()
    assert result["connection_error"] == worker.connection_error
    assert not result["grid_connected"]
    assert Settings.GRID_API_KEY not in json.dumps(result)


@pytest.mark.asyncio
async def test_missing_expected_connection_never_reports_online(monkeypatch):
    worker = SimpleNamespace(connected=True, connection_error=None, session_stats=lambda: {})
    monkeypatch.setitem(worker_state, "running", True)
    monkeypatch.setitem(worker_state, "workers", {"worker-a": worker})
    monkeypatch.setitem(worker_state, "expected_workers", {"worker-a", "worker-b"})
    result = await api_status()
    assert result["connected_workers"] == 1
    assert result["total_workers"] == 2
    assert result["grid_connected"] is False


def test_session_stats_aggregate_every_active_connection():
    workers = [
        SimpleNamespace(session_stats=lambda: {
            "jobs_completed": 2, "den_earned": 3.5,
            "jobs_per_hour": 0, "den_per_hour": 0, "uptime_seconds": 1800,
        }),
        SimpleNamespace(session_stats=lambda: {
            "jobs_completed": 1, "den_earned": 1.5,
            "jobs_per_hour": 0, "den_per_hour": 0, "uptime_seconds": 3600,
        }),
    ]
    assert _aggregate_session_stats(workers) == {
        "jobs_completed": 3,
        "den_earned": 5.0,
        "jobs_per_hour": 3.0,
        "den_per_hour": 5.0,
        "uptime_seconds": 3600.0,
    }
