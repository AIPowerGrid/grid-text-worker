# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ask a backend for tool-calling requests in one piece instead of a stream.

Some model servers parse tool calls correctly when asked for a complete
response but not when streaming: the call's arguments arrive inside the
reasoning text, with no `tool_calls` and `finish_reason: "stop"`. An agent
then sees an empty answer. vLLM 0.10.2 with gpt-oss does this on longer tasks.

For the backends an operator names, a request that carries `tools` is sent
with `stream: false` and the complete answer is replayed as the stream lines
the job loop already reads. Everything else keeps streaming.

Costs, by design: the caller of such a request sees no tokens until the answer
is complete, and a cancel only takes effect once the backend has finished.
"""

from __future__ import annotations

import json
import os

import httpx

ENV = "GRID_TOOLS_NONSTREAM_MODELS"
# One complete answer can take far longer than the gap between stream chunks.
TIMEOUT = httpx.Timeout(600.0, connect=10.0)


def models() -> frozenset[str]:
    """Backend model names (as configured for this worker) that need this."""
    return frozenset(name.strip() for name in os.getenv(ENV, "").split(",") if name.strip())


def applies(model_name: str, request: dict) -> bool:
    return bool(request.get("tools")) and model_name in models()


def _same_json(left: str, right: str) -> bool:
    try:
        return json.loads(left) == json.loads(right)
    except (TypeError, ValueError):
        return left.strip() == right.strip()


def stream_lines(body: dict) -> list[str]:
    """Replay one complete chat completion as the SSE lines of a stream."""
    lines: list[str] = []
    choices = body.get("choices") or []
    if choices:
        choice = choices[0]
        message = choice.get("message") or {}
        delta: dict = {}
        tool_calls = message.get("tool_calls") or []
        content = message.get("content")
        # The same server repeats a tool call's arguments as the visible
        # answer. That text is not an answer; relaying it would show raw JSON.
        if isinstance(content, str) and tool_calls and any(
            _same_json(content, (call.get("function") or {}).get("arguments") or "")
            for call in tool_calls
        ):
            content = None
        if content:
            delta["content"] = content
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            delta["reasoning_content"] = reasoning
        if message.get("refusal"):
            delta["refusal"] = message["refusal"]
        if tool_calls:
            delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(tool_calls)]
        chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": choice.get("finish_reason")}]}
        if choice.get("logprobs") is not None:
            chunk["choices"][0]["logprobs"] = choice["logprobs"]
        lines.append("data: " + json.dumps(chunk))
    if body.get("usage"):
        lines.append("data: " + json.dumps({"choices": [], "usage": body["usage"]}))
    lines.append("data: [DONE]")
    return lines


class CompleteAsStream:
    """`async with` stand-in for `client.stream(...)` on a non-streaming call."""

    def __init__(self, client: httpx.AsyncClient, url: str, request: dict, headers: dict):
        self._client, self._url, self._headers = client, url, headers
        self._request = {k: v for k, v in request.items() if k != "stream_options"}
        self._request["stream"] = False
        self._response: httpx.Response | None = None
        self.status_code = 0

    async def __aenter__(self) -> "CompleteAsStream":
        self._response = await self._client.post(
            self._url, json=self._request, headers=self._headers, timeout=TIMEOUT,
        )
        self.status_code = self._response.status_code
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def aread(self) -> bytes:
        return self._response.content if self._response is not None else b""

    async def aiter_lines(self):
        try:
            body = self._response.json() if self._response is not None else {}
        except ValueError:
            body = {}
        for line in stream_lines(body if isinstance(body, dict) else {}):
            yield line
