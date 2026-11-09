# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real watcher + HTTPX transport fixtures; no live Core, GPU, or economic proof."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from inference_worker import ws_client
from inference_worker.config import Settings


PROTOCOLS = [
    ("openai-chat", True),
    ("openai-responses", True),
    ("anthropic", True),
    ("openai-responses", False),
    ("anthropic", False),
]
USAGE = {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []
        self.received = []
        self.readers = 0
        self.terminal_readers = []
        self.on_terminal = None

    async def recv(self):
        self.readers += 1
        try:
            message = await self.queue.get()
            if isinstance(message, Exception):
                raise message
            self.received.append((message, asyncio.current_task()))
            return json.dumps(message)
        finally:
            self.readers -= 1

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["type"] not in {"done", "error"}:
            return
        self.terminal_readers.append(self.readers)
        if message["type"] == "done":
            self.queue.put_nowait({"type": "ack", "id": message["id"], "den": 0})
        if self.on_terminal:
            self.on_terminal(message)


@pytest_asyncio.fixture
async def worker(monkeypatch):
    monkeypatch.setattr(Settings, "validate", lambda: None)
    monkeypatch.setattr(ws_client, "get_signer", lambda: None)
    w = ws_client.StreamingWorker(spec=SimpleNamespace(
        name="fixture", model_name="fixture", grid_model_name="fixture",
        modalities=["text"], modalities_declared=True, backend_type="openai",
        url="http://127.0.0.1:8000/v1", api_key="", concurrency=1, schedule="",
    ))
    w.ws = Socket()
    monkeypatch.setattr(w, "_sign_receipt", lambda *_: None)
    try:
        yield w
    finally:
        await w.backend.aclose()


async def install_backend(worker, handler):
    await worker.backend.aclose()
    worker.backend = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def job(api_format, stream, job_id="first-job"):
    return {"type": "job", "id": job_id, "payload": {
        "api_format": api_format,
        "request": {
            "messages": [{"role": "user", "content": "fixture"}],
            "stream": stream, "max_tokens": 32,
        },
    }}


def completed_response(api_format, stream):
    if not stream:
        return httpx.Response(200, json={"output": [{"text": "hello"}], "usage": USAGE})
    chunk = {"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}], "usage": USAGE}
    return httpx.Response(200, content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("api_format,stream", PROTOCOLS)
async def test_timeout_does_not_steal_next_job(worker, monkeypatch, api_format, stream):
    health_started = asyncio.Event()
    release_health = asyncio.Event()
    second_done = asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("fixture", request=request)
        return completed_response(api_format, stream)

    async def healthy():
        health_started.set()
        await release_health.wait()
        return True

    def terminal(message):
        if message["type"] == "error":
            worker.ws.queue.put_nowait(job(api_format, stream, "next-job"))
        if message["type"] == "done":
            second_done.set()

    await install_backend(worker, handler)
    monkeypatch.setattr(worker, "_backend_healthy", healthy)
    worker.ws.on_terminal = terminal
    worker.ws.queue.put_nowait(job(api_format, stream))
    loop = asyncio.create_task(worker._message_loop())
    try:
        await asyncio.wait_for(health_started.wait(), 1)
        await asyncio.sleep(0)
        assert worker.ws.queue.qsize() == 1
        assert worker.ws.terminal_readers == [0]
        release_health.set()
        await asyncio.wait_for(second_done.wait(), 1)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    assert calls == 2
    assert [m["type"] for m in worker.ws.sent if m["type"] in {"done", "error"}] == ["error", "done"]
    assert all(reader is loop for m, reader in worker.ws.received if m.get("type") == "job")
    assert worker.ws.terminal_readers == [0, 0]
    assert worker.ws.readers == 0


class StalledBackend:
    def __init__(self, api_format, stream, phase):
        self.api_format = api_format
        self.stream = stream
        self.phase = phase
        self.ready = asyncio.Event()
        self.closed = False
        self.tasks = set()

    async def __call__(self, request):
        self.tasks.add(asyncio.current_task())
        if self.phase == "headers":
            try:
                self.ready.set()
                await asyncio.Event().wait()
            finally:
                self.closed = True
        return httpx.Response(200, stream=StalledBody(self))


class StalledBody(httpx.AsyncByteStream):
    def __init__(self, backend):
        self.backend = backend

    async def __aiter__(self):
        if self.backend.phase == "partial":
            if not self.backend.stream:
                yield b'{"output": ['
            elif self.backend.api_format == "openai-chat":
                chunk = {"choices": [{"delta": {"content": "hello"}}], "usage": USAGE}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            else:
                yield ("event: fixture\ndata: " + json.dumps({"text": "hello", "usage": USAGE}) + "\n\n").encode()
        self.backend.ready.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.backend.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("api_format,stream", PROTOCOLS)
@pytest.mark.parametrize("phase", ["headers", "silent", "partial"])
async def test_cancel_closes_pending_backend_and_preserves_real_output(worker, api_format, stream, phase):
    backend = StalledBackend(api_format, stream, phase)
    await install_backend(worker, backend)
    task = asyncio.create_task(worker._handle_job(job(api_format, stream)))
    try:
        await asyncio.wait_for(backend.ready.wait(), 1)
        worker.ws.queue.put_nowait({"type": "cancel", "id": "first-job"})
        await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert backend.closed
    assert all(t.done() for t in backend.tasks)
    assert worker.ws.readers == 0
    terminal = [m for m in worker.ws.sent if m["type"] in {"done", "error"}]
    assert len(terminal) == 1 and terminal[0]["type"] == "done"
    done = terminal[0]
    assert done["cancelled"] is True
    assert worker.ws.terminal_readers == [0]
    if phase == "partial" and stream:
        assert done["usage"] == USAGE
        assert any(m["type"] in {"token", "raw"} for m in worker.ws.sent)
        if api_format == "openai-chat":
            assert done["full_text"] == "hello"
    else:
        assert not done.get("usage")
        assert not done.get("full_json")
        assert not done.get("full_text")


@pytest.mark.asyncio
@pytest.mark.parametrize("api_format,stream", PROTOCOLS)
@pytest.mark.parametrize("stop", ["shutdown", "disconnect", "unexpected_job"])
async def test_shutdown_and_disconnect_cleanup_without_fake_terminal(worker, api_format, stream, stop):
    backend = StalledBackend(api_format, stream, "headers")
    await install_backend(worker, backend)
    task = asyncio.create_task(worker._handle_job(job(api_format, stream)))
    try:
        await asyncio.wait_for(backend.ready.wait(), 1)
        if stop == "shutdown":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        else:
            worker.ws.queue.put_nowait(
                job(api_format, stream, "unexpected-job") if stop == "unexpected_job"
                else ConnectionError("fixture disconnect")
            )
            with pytest.raises(ConnectionError, match="Grid connection ended"):
                await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert backend.closed
    assert all(t.done() for t in backend.tasks)
    assert worker.ws.readers == 0
    assert not any(m["type"] in {"done", "error"} for m in worker.ws.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_format,stream", PROTOCOLS)
@pytest.mark.parametrize("status", [400, 503])
async def test_http_failure_keeps_next_assignment_for_main_reader(worker, api_format, stream, status):
    response = httpx.Response(status, content="fixture rejection")
    await install_backend(worker, lambda _: response)
    worker.ws.on_terminal = lambda _: worker.ws.queue.put_nowait(job(api_format, stream, "next-job"))
    if status == 400:
        await asyncio.wait_for(worker._handle_job(job(api_format, stream)), 1)
    else:
        with pytest.raises(ws_client.BackendUnavailable):
            await asyncio.wait_for(worker._handle_job(job(api_format, stream)), 1)
    assert response.is_closed
    assert worker.ws.terminal_readers == [0]
    assert json.loads(await asyncio.wait_for(worker.ws.recv(), 1))["id"] == "next-job"
    assert worker.ws.sent[-1]["client_error"] is (status == 400)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_format,stream", PROTOCOLS)
async def test_shutdown_wins_race_with_completed_ack(worker, api_format, stream):
    await install_backend(worker, lambda _: completed_response(api_format, stream))
    receive = worker.ws.recv
    loop_task = None

    async def receive_and_cancel():
        raw = await receive()
        if json.loads(raw).get("type") == "ack":
            asyncio.get_running_loop().call_soon(loop_task.cancel)
        return raw

    worker.ws.recv = receive_and_cancel
    worker.ws.queue.put_nowait(job(api_format, stream))
    loop_task = asyncio.create_task(worker._message_loop())
    try:
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await loop_task
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
    assert worker.ws.readers == 0
    assert loop_task.cancelled()
    assert worker.ws.terminal_readers == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("api_format,stream", PROTOCOLS)
async def test_wrong_job_cancel_does_not_abort_current_request(worker, api_format, stream):
    backend = StalledBackend(api_format, stream, "headers")
    await install_backend(worker, backend)
    task = asyncio.create_task(worker._handle_job(job(api_format, stream)))
    try:
        await asyncio.wait_for(backend.ready.wait(), 1)
        worker.ws.queue.put_nowait({"type": "cancel", "id": "another-job"})
        await asyncio.sleep(0)
        assert not task.done()
        assert not backend.closed
        worker.ws.queue.put_nowait({"type": "cancel", "id": "first-job"})
        await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert backend.closed
    assert worker.ws.readers == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("api_format,stream", PROTOCOLS)
async def test_successful_terminal_does_not_steal_next_job(worker, api_format, stream):
    await install_backend(worker, lambda _: completed_response(api_format, stream))
    second_done = asyncio.Event()

    def terminal(message):
        if message["id"] == "first-job":
            worker.ws.queue.put_nowait(job(api_format, stream, "next-job"))
        else:
            second_done.set()

    worker.ws.on_terminal = terminal
    worker.ws.queue.put_nowait(job(api_format, stream))
    loop = asyncio.create_task(worker._message_loop())
    try:
        await asyncio.wait_for(second_done.wait(), 1)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    assert worker.ws.terminal_readers == [0, 0]
    assert all(reader is loop for m, reader in worker.ws.received if m.get("type") == "job")
    assert worker.ws.readers == 0


@pytest.mark.asyncio
async def test_unexpected_assignment_is_not_ignored_when_backend_also_finishes(worker):
    async def complete():
        return None

    worker.ws.queue.put_nowait(job("openai-chat", True, "unexpected-job"))
    with pytest.raises(ConnectionError, match="Grid connection ended"):
        await worker._run_backend_operation("first-job", complete())
    assert worker.ws.readers == 0
