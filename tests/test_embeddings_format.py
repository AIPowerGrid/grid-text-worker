# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`openai-embeddings`: format probe, registration of an embedding-only backend
and the raw job relay. Local tests with a faked backend; not a live Grid proof."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from inference_worker import ws_client
from inference_worker.config import Settings

FMT = ws_client.EMBEDDINGS_FORMAT
CHAT_OK = {"choices": [{"message": {"content": "hi"}}]}
EMBED_OK = {"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "usage": {"prompt_tokens": 2, "total_tokens": 2}}


class Backend:
    """Stands in for httpx.AsyncClient: answers per endpoint suffix and records calls."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []
        self.timeouts: dict[str, object] = {}

    async def post(self, url, json=None, headers=None, timeout=None):
        suffix = url.split("/v1/", 1)[1] if "/v1/" in url else url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1]
        self.calls.append((suffix, json))
        self.timeouts[suffix] = timeout
        answer = self.answers.get(suffix, (404, {"error": "not found"}))
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        if isinstance(body, (bytes, str)):
            return httpx.Response(status, content=body, request=httpx.Request("POST", url))
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    def probed(self) -> list[str]:
        return [suffix for suffix, _ in self.calls]


class GridSocket:
    """Grid side of the worker socket: silent during a job, acks after `done`."""

    def __init__(self):
        self.sent: list[dict] = []
        self._terminal = asyncio.Event()

    async def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        if message["type"] in ("done", "error"):
            self._terminal.set()

    async def recv(self):
        await self._terminal.wait()
        self._terminal.clear()
        return json.dumps({"type": "ack", "id": self.sent[-1]["id"], "den": 0.1})


def make_worker(monkeypatch, answers, *, declared=False, backend_type="openai", modalities=("text",)):
    monkeypatch.setattr(Settings, "validate", lambda: None)
    monkeypatch.setattr(ws_client, "get_signer", lambda: None)
    backend = Backend(answers)
    monkeypatch.setattr(ws_client.httpx, "AsyncClient", lambda **kwargs: backend)
    spec = SimpleNamespace(
        name="embed-worker", model_name="nomic-embed-text", grid_model_name="nomic-embed-text",
        modalities=list(modalities), modalities_declared=declared, backend_type=backend_type,
        url="http://127.0.0.1:8000" if backend_type == "ollama" else "http://127.0.0.1:8000/v1",
        api_key="", concurrency=1, schedule="",
    )
    worker = ws_client.StreamingWorker(spec=spec)
    return worker, backend


@pytest.mark.parametrize("body,ok", [
    (EMBED_OK, True),
    ({"data": [{"embedding": "AACAPw=="}]}, False),      # base64 was not asked for
    ({"data": [{"embedding": [None]}]}, False),
    ({"data": [{"embedding": [0.1, "x"]}]}, False),
    ({"data": [{"embedding": []}]}, False),
    ({"data": [{"embedding": ""}]}, False),
    ({"data": [{"embedding": 3}]}, False),
    ({"data": []}, False),
    ({"data": [{}]}, False),
    ({}, False),
    (None, False),
])
def test_embedding_probe_requires_a_vector(body, ok):
    assert ws_client._embedding_probe_ok(body) is ok


@pytest.mark.asyncio
async def test_probe_finds_an_embedding_only_backend(monkeypatch):
    worker, backend = make_worker(monkeypatch, {
        "chat/completions": (400, {"error": "model does not support chat"}),
        "embeddings": (200, EMBED_OK),
    })
    assert await worker._probe_formats() == [FMT]
    assert ("embeddings", {"model": "nomic-embed-text", "input": "hi"}) in backend.calls


@pytest.mark.asyncio
async def test_a_chat_backend_is_never_asked_for_embeddings(monkeypatch):
    """Older Ollama answers /v1/embeddings for chat models; a chat backend must start as before."""
    worker, backend = make_worker(monkeypatch, {
        "chat/completions": (200, CHAT_OK),
        "embeddings": (200, EMBED_OK),
    })
    assert await worker._probe_formats() == ["openai-chat"]
    assert backend.probed() == ["chat/completions", "responses", "messages"]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_answer", [
    httpx.ReadTimeout("cold load"),
    httpx.ConnectError("refused"),
    (500, {"error": "boom"}),
    (503, {"error": "loading"}),
    (200, {"choices": [{"message": {"content": ""}}]}),       # answered, but produced nothing
])
async def test_a_chat_probe_that_merely_failed_never_turns_into_an_embeddings_worker(monkeypatch, chat_answer):
    worker, backend = make_worker(monkeypatch, {"chat/completions": chat_answer, "embeddings": (200, EMBED_OK)})
    assert await worker._probe_formats() == []
    assert "embeddings" not in backend.probed()
    monkeypatch.setattr(worker, "_backend_healthy", AsyncMock(return_value=True))
    connect = AsyncMock()
    monkeypatch.setattr(ws_client.websockets, "connect", connect)
    with pytest.raises(ws_client.BackendUnavailable):
        await worker.connect()
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_embeddings_probe_gets_the_cold_load_timeout(monkeypatch):
    worker, backend = make_worker(monkeypatch, {
        "chat/completions": (400, {"error": "does not support chat"}), "embeddings": (200, EMBED_OK)})
    await worker._probe_formats()
    assert backend.timeouts["embeddings"] == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities,expected", [
    (["embedding"], ["openai-embeddings"]),
    (["completion", "tools"], []),                 # a chat model that happens to answer /v1/embeddings
])
async def test_ollama_advertises_embeddings_only_for_embedding_models(monkeypatch, capabilities, expected):
    worker, backend = make_worker(monkeypatch, {
        "chat/completions": (400, {"error": "does not support chat"}),
        "embeddings": (200, EMBED_OK),
        "api/show": (200, {"capabilities": capabilities}),
    }, backend_type="ollama")
    assert await worker._probe_formats() == expected
    assert ("embeddings" in backend.probed()) is bool(expected)


@pytest.mark.asyncio
async def test_ollama_without_a_capability_answer_falls_back_to_the_probe(monkeypatch):
    worker, _ = make_worker(monkeypatch, {
        "chat/completions": (400, {"error": "does not support chat"}),
        "embeddings": (200, EMBED_OK),
    }, backend_type="ollama")                                  # /api/show answers 404 here
    assert await worker._probe_formats() == [FMT]


@pytest.mark.asyncio
async def test_probe_drops_embeddings_without_a_vector(monkeypatch):
    worker, _ = make_worker(monkeypatch, {
        "chat/completions": (400, {"error": "no chat"}),
        "embeddings": (200, {"object": "list", "data": []}),
    })
    assert await worker._probe_formats() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"<html>catch-all page</html>", {"error": "unknown path"}, [1, 2]])
async def test_odd_200_answers_never_advertise_embeddings(monkeypatch, body):
    worker, _ = make_worker(monkeypatch, {
        "chat/completions": (404, {"error": "nope"}), "embeddings": (200, body)})
    assert await worker._probe_formats() == []


@pytest.mark.asyncio
async def test_declared_modalities_win_for_an_embeddings_backend(monkeypatch):
    worker, _ = make_worker(monkeypatch, {}, declared=True, modalities=("text", "image"))
    monkeypatch.setattr(worker, "_backend_healthy", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_probe_formats", AsyncMock(return_value=[FMT]))
    monkeypatch.setattr(worker, "_detect_context", AsyncMock(return_value=8192))
    ws = AsyncMock()
    ws.recv.return_value = json.dumps({"type": "ready", "worker_id": "worker-embed"})
    monkeypatch.setattr(ws_client.websockets, "connect", AsyncMock(return_value=ws))
    await worker.connect()
    assert json.loads(ws.send.await_args_list[0].args[0])["modalities"] == ["text", "image"]


@pytest.mark.asyncio
async def test_embedding_only_backend_registers_with_that_one_format(monkeypatch):
    worker, _ = make_worker(monkeypatch, {})
    monkeypatch.setattr(worker, "_backend_healthy", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_probe_formats", AsyncMock(return_value=[FMT]))
    monkeypatch.setattr(worker, "_detect_context", AsyncMock(return_value=8192))
    vision = AsyncMock(side_effect=AssertionError("an embedding model must not be vision-probed"))
    monkeypatch.setattr(worker, "_detect_vision", vision)
    ws = AsyncMock()
    ws.recv.return_value = json.dumps({"type": "ready", "worker_id": "worker-embed"})
    monkeypatch.setattr(ws_client.websockets, "connect", AsyncMock(return_value=ws))

    await worker.connect()

    registration = json.loads(ws.send.await_args_list[0].args[0])
    assert registration["api_formats"] == [FMT]
    assert registration["models"] == ["nomic-embed-text"]
    assert registration["modalities"] == ["text"]
    assert worker.connected
    vision.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("formats", [[], ["openai-responses"], ["anthropic", FMT]])
async def test_backend_without_chat_or_embeddings_only_still_refuses(monkeypatch, formats):
    worker, _ = make_worker(monkeypatch, {})
    monkeypatch.setattr(worker, "_backend_healthy", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_probe_formats", AsyncMock(return_value=formats))
    connect = AsyncMock()
    monkeypatch.setattr(ws_client.websockets, "connect", connect)
    with pytest.raises(ws_client.BackendUnavailable):
        await worker.connect()
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_embeddings_job_posts_once_and_returns_the_body(monkeypatch):
    worker, backend = make_worker(monkeypatch, {"embeddings": (200, EMBED_OK)})
    worker.ws = GridSocket()
    payload = {"api_format": FMT, "max_length": 0,
               "request": {"model": "grid-name", "input": ["a", "b"], "stream": True}}

    await worker._handle_raw_job("job-1", FMT, payload)

    assert backend.probed() == ["embeddings"]             # exactly one request
    suffix, sent = backend.calls[-1]
    assert sent == {"model": "nomic-embed-text", "input": ["a", "b"]}    # `stream` is not forwarded
    messages = worker.ws.sent
    done = next(m for m in messages if m["type"] == "done")
    assert done["id"] == "job-1" and done["full_json"] == EMBED_OK
    assert not any(m["type"] == "raw" for m in messages)  # never relayed as a stream


@pytest.mark.asyncio
async def test_backend_rejection_is_reported_as_a_client_error(monkeypatch):
    worker, _ = make_worker(monkeypatch, {"embeddings": (400, {"error": "input too long"})})
    worker.ws = GridSocket()
    payload = {"api_format": FMT, "request": {"model": "grid-name", "input": "x"}}

    await worker._handle_raw_job("job-2", FMT, payload)

    messages = worker.ws.sent
    error = next(m for m in messages if m["type"] == "error")
    assert error["client_error"] is True and "HTTP 400" in error["message"]
    assert not any(m["type"] == "done" for m in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    (200, b"<html>not json</html>"),
    httpx.ReadError("connection reset"),
])
async def test_a_result_is_always_submitted_when_the_backend_answer_is_unusable(monkeypatch, answer):
    worker, _ = make_worker(monkeypatch, {"embeddings": answer})
    worker.ws = GridSocket()
    await worker._handle_raw_job("job-4", FMT, {"api_format": FMT, "request": {"model": "m", "input": "x"}})
    assert [m["type"] for m in worker.ws.sent] == ["error"]
    assert "no usable answer" in worker.ws.sent[0]["message"]


# ── Ollama: native endpoint, no silent truncation ────────────────────────────


@pytest.mark.asyncio
async def test_ollama_embeddings_go_to_the_native_endpoint_and_come_back_in_openai_shape(monkeypatch):
    native = {"model": "nomic-embed-text", "embeddings": [[0.5, -0.25], [1.0, 0.0]], "prompt_eval_count": 7}
    worker, backend = make_worker(monkeypatch, {"api/embed": (200, native)}, backend_type="ollama")
    worker.ws = GridSocket()
    request = {"model": "grid-name", "input": ["a", "b"], "dimensions": 2, "stream": True}
    await worker._handle_raw_job("job-5", FMT, {"api_format": FMT, "request": request})
    assert backend.calls == [("api/embed", {"model": "nomic-embed-text", "input": ["a", "b"],
                                            "truncate": False, "dimensions": 2})]
    done = next(m for m in worker.ws.sent if m["type"] == "done")
    assert done["full_json"]["data"] == [
        {"object": "embedding", "index": 0, "embedding": [0.5, -0.25]},
        {"object": "embedding", "index": 1, "embedding": [1.0, 0.0]},
    ]
    assert done["full_json"]["usage"] == {"prompt_tokens": 7, "total_tokens": 7}


@pytest.mark.asyncio
async def test_ollama_base64_embeddings_are_little_endian_float32(monkeypatch):
    worker, _ = make_worker(monkeypatch, {"api/embed": (200, {"embeddings": [[1.0, -2.0]]})}, backend_type="ollama")
    worker.ws = GridSocket()
    request = {"model": "m", "input": "a", "encoding_format": "base64"}
    await worker._handle_raw_job("job-6", FMT, {"api_format": FMT, "request": request})
    done = next(m for m in worker.ws.sent if m["type"] == "done")
    assert done["full_json"]["data"][0]["embedding"] == "AACAPwAAAMA="      # 1.0, -2.0 as <f4


@pytest.mark.asyncio
async def test_an_input_longer_than_the_context_is_a_client_error_not_a_cut_off_vector(monkeypatch):
    refusal = (400, {"error": "the input length exceeds the context length"})
    worker, _ = make_worker(monkeypatch, {"api/embed": refusal}, backend_type="ollama")
    worker.ws = GridSocket()
    await worker._handle_raw_job("job-7", FMT, {"api_format": FMT, "request": {"model": "m", "input": "x" * 9000}})
    assert [m["type"] for m in worker.ws.sent] == ["error"]
    assert worker.ws.sent[0].get("client_error") is True
    assert "exceeds the context length" in worker.ws.sent[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [
    {}, {"embeddings": []}, {"embeddings": "x"}, {"embeddings": [[]]}, {"embeddings": [[0.1, "x"]]},
    {"embeddings": [[True]]}, [1, 2],
])
async def test_an_unusable_native_answer_still_submits_an_error(monkeypatch, native):
    worker, _ = make_worker(monkeypatch, {"api/embed": (200, native)}, backend_type="ollama")
    worker.ws = GridSocket()
    await worker._handle_raw_job("job-8", FMT, {"api_format": FMT, "request": {"model": "m", "input": "x"}})
    assert [m["type"] for m in worker.ws.sent] == ["error"]


@pytest.mark.asyncio
async def test_other_engines_are_forwarded_to_the_openai_endpoint_unchanged(monkeypatch):
    worker, backend = make_worker(monkeypatch, {"embeddings": (200, EMBED_OK)})
    worker.ws = GridSocket()
    await worker._handle_raw_job("job-9", FMT, {"api_format": FMT, "request": {"model": "m", "input": "x"}})
    assert backend.probed() == ["embeddings"]
