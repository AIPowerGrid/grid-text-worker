# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in complete-answer path for tool requests; mock HTTP, no live backend."""

import asyncio
import json

import httpx
import pytest

from inference_worker import tool_fallback
from tests.test_job_lifecycle import USAGE, install_backend, job
from tests.test_job_lifecycle import worker as worker  # noqa: F401

TOOLS = [{"type": "function", "function": {"name": "shell", "parameters": {"type": "object"}}}]
ARGS = '{"command":"ls -R"}'
CALL = {"id": "call-1", "type": "function", "function": {"name": "shell", "arguments": ARGS}}


def complete(message, finish="tool_calls"):
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": USAGE}


def test_only_named_backends_with_tools_are_affected(monkeypatch):
    monkeypatch.delenv(tool_fallback.ENV, raising=False)
    assert not tool_fallback.applies("gpt-oss-120b", {"tools": TOOLS})
    monkeypatch.setenv(tool_fallback.ENV, "other, gpt-oss-120b")
    assert tool_fallback.applies("gpt-oss-120b", {"tools": TOOLS})
    assert not tool_fallback.applies("gpt-oss-120b", {"messages": []})
    assert not tool_fallback.applies("gpt-oss-120b", {"tools": []})
    assert not tool_fallback.applies("qwen", {"tools": TOOLS})


def test_complete_tool_answer_replays_as_one_tool_delta_without_the_echoed_arguments():
    lines = tool_fallback.stream_lines(complete(
        {"role": "assistant", "content": '{ "command": "ls -R" }', "reasoning_content": "list files", "tool_calls": [CALL]},
    ))
    first, usage, done = lines
    choice = json.loads(first[6:])["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["delta"] == {"reasoning_content": "list files", "tool_calls": [{"index": 0, **CALL}]}
    assert json.loads(usage[6:]) == {"choices": [], "usage": USAGE}
    assert done == "data: [DONE]"


def test_real_content_next_to_a_tool_call_and_plain_answers_are_kept():
    with_text = tool_fallback.stream_lines(complete({"content": "Checking the folder.", "tool_calls": [CALL]}))
    assert json.loads(with_text[0][6:])["choices"][0]["delta"]["content"] == "Checking the folder."
    plain = tool_fallback.stream_lines(complete({"content": "391", "reasoning": "17*23"}, "stop"))
    assert json.loads(plain[0][6:])["choices"][0] == {
        "index": 0, "delta": {"content": "391", "reasoning_content": "17*23"}, "finish_reason": "stop",
    }


async def run_one(worker, request_extra):  # noqa: F811
    seen = []

    async def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if body["stream"]:
            # The broken streaming shape: arguments inside the reasoning text.
            chunk = {"choices": [{"delta": {"reasoning_content": "list files" + ARGS}, "finish_reason": "stop"}], "usage": USAGE}
            return httpx.Response(200, content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json=complete({"content": ARGS, "reasoning_content": "list files", "tool_calls": [CALL]}))

    await install_backend(worker, handler)
    finished = asyncio.Event()
    worker.ws.on_terminal = lambda _message: finished.set()
    assignment = job("openai-chat", True)
    assignment["payload"]["request"].update(request_extra)
    worker.ws.queue.put_nowait(assignment)
    loop = asyncio.create_task(worker._message_loop())
    try:
        await asyncio.wait_for(finished.wait(), 1)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    return seen, worker.ws.sent


@pytest.mark.asyncio
async def test_named_backend_gets_a_complete_request_and_relays_a_real_tool_call(worker, monkeypatch):  # noqa: F811
    monkeypatch.setenv(tool_fallback.ENV, "fixture")
    seen, sent = await run_one(worker, {"tools": TOOLS})

    assert [body["stream"] for body in seen] == [False] and "stream_options" not in seen[0]
    assert seen[0]["tools"] == TOOLS and seen[0]["model"] == "fixture"
    tokens = [m for m in sent if m["type"] == "token"]
    assert len(tokens) == 1 and tokens[0]["finish_reason"] == "tool_calls"
    assert tokens[0]["delta"] == {"reasoning_content": "list files", "tool_calls": [{"index": 0, **CALL}]}
    done = [m for m in sent if m["type"] == "done"][0]
    assert done["finish_reason"] == "tool_calls" and done["full_text"] == "" and done["usage"] == USAGE


@pytest.mark.asyncio
@pytest.mark.parametrize("named,extra", [("fixture", {}), ("", {"tools": TOOLS})])
async def test_other_requests_keep_streaming(worker, monkeypatch, named, extra):  # noqa: F811
    monkeypatch.setenv(tool_fallback.ENV, named)
    seen, sent = await run_one(worker, extra)

    assert [body["stream"] for body in seen] == [True]
    assert [m for m in sent if m["type"] == "done"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_backend_rejection_on_the_complete_path_fails_only_that_job(worker, monkeypatch):  # noqa: F811
    monkeypatch.setenv(tool_fallback.ENV, "fixture")
    await install_backend(worker, lambda _request: httpx.Response(400, json={"error": "bad tool schema"}))
    finished = asyncio.Event()
    worker.ws.on_terminal = lambda _message: finished.set()
    assignment = job("openai-chat", True)
    assignment["payload"]["request"]["tools"] = TOOLS
    worker.ws.queue.put_nowait(assignment)
    loop = asyncio.create_task(worker._message_loop())
    try:
        await asyncio.wait_for(finished.wait(), 1)
        assert not loop.done()
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    error = [m for m in worker.ws.sent if m["type"] == "error"][0]
    assert "bad tool schema" in json.dumps(error)
