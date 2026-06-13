# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebSocket streaming client for the Grid Streaming API.

Replaces HTTP polling with a persistent WebSocket connection.
The worker receives jobs pushed from the server and streams tokens
back in real time as the inference engine generates them.

Enable with GRID_STREAMING=true in your .env.
"""

import asyncio
import json
import logging
import time

import httpx
import websockets

from .config import Settings

logger = logging.getLogger(__name__)

BRIDGE_AGENT = "Grid Inference Worker:2.0.0:streaming"


def _strip_thinking_tags(text: str) -> str:
    """Remove think-tag blocks from reasoning models."""
    import re
    if not text:
        return text
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL).strip()


class StreamingWorker:
    """WebSocket-based worker that streams tokens to the Grid API."""

    def __init__(self):
        Settings.validate()
        self.model_name: str = Settings.MODEL_NAME
        self.grid_model_name: str = Settings.GRID_MODEL_NAME or self._build_grid_model_name()
        self.backend = httpx.AsyncClient(timeout=120)
        self.ws = None
        self.worker_id = None
        self._reconnect_delay = 1
        self._jobs_completed = 0
        self._total_den = 0.0

    def _build_grid_model_name(self) -> str:
        if Settings.BACKEND_TYPE == "ollama":
            return f"grid/{self.model_name}"
        return f"grid/{self.model_name}"

    def _get_completions_url(self) -> str:
        if Settings.BACKEND_TYPE == "ollama":
            return f"{Settings.OLLAMA_URL}/v1/chat/completions"
        return f"{Settings.OPENAI_URL}/chat/completions"

    def _get_auth_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if Settings.BACKEND_TYPE != "ollama" and Settings.OPENAI_API_KEY:
            headers["Authorization"] = f"Bearer {Settings.OPENAI_API_KEY}"
        return headers

    @property
    def ws_url(self) -> str:
        """Build WebSocket URL from the Grid API URL."""
        api_url = Settings.GRID_STREAMING_URL or Settings.GRID_API_URL
        # Convert http(s) to ws(s)
        ws_url = api_url.replace("https://", "wss://").replace("http://", "ws://")
        # Strip /api suffix if present
        ws_url = ws_url.rstrip("/")
        if ws_url.endswith("/api"):
            ws_url = ws_url[:-4]
        return f"{ws_url}/v1/workers/ws"

    async def connect(self):
        """Connect to the Grid Streaming API via WebSocket."""
        logger.info(f"Connecting to {self.ws_url}...")

        self.ws = await websockets.connect(
            self.ws_url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        )

        # Send registration message
        await self.ws.send(json.dumps({
            "apikey": Settings.GRID_API_KEY,
            "name": Settings.GRID_WORKER_NAME,
            "models": [self.grid_model_name],
            "max_length": Settings.MAX_LENGTH,
            "max_context_length": Settings.MAX_CONTEXT_LENGTH,
            "bridge_agent": BRIDGE_AGENT,
        }))

        # Wait for ready response
        response = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
        if response.get("type") == "error":
            raise ConnectionRefusedError(f"Server rejected connection: {response.get('message')}")

        if response.get("type") == "ready":
            self.worker_id = response["worker_id"]
            self._reconnect_delay = 1  # Reset backoff on success
            logger.info(f"Connected as worker {self.worker_id}")
        else:
            raise ConnectionError(f"Unexpected response: {response}")

    async def run(self):
        """Main loop with auto-reconnect."""
        while True:
            try:
                await self.connect()
                await self._message_loop()
            except (websockets.ConnectionClosed, ConnectionRefusedError, ConnectionError, OSError) as e:
                logger.warning(f"Connection lost: {e}. Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30)
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _message_loop(self):
        """Process messages from the server."""
        while True:
            raw = await self.ws.recv()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "job":
                await self._handle_job(msg)
            elif msg_type == "ping":
                await self.ws.send(json.dumps({"type": "pong"}))
            elif msg_type == "no_job":
                pass  # Server will push next job when available
            elif msg_type == "error":
                logger.error(f"Server error: {msg.get('message')}")
            else:
                logger.debug(f"Unknown message type: {msg_type}")

    async def _handle_job(self, job: dict):
        """Process a job: stream inference tokens back to the server."""
        job_id = job["id"]
        model = job.get("model", self.model_name)
        payload = job.get("payload", {})

        prompt = payload.get("prompt", "")
        max_tokens = int(payload.get("max_length", 512))
        temperature = float(payload.get("temperature", 0.7))
        top_p = float(payload.get("top_p", 0.9))

        logger.info(f"📥 Job {job_id[:8]} | model={model} | max_tokens={max_tokens}")

        # Build the OpenAI-compatible request with streaming enabled
        openai_payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,  # Enable streaming from the inference engine
        }

        if Settings.REASONING_EFFORT:
            openai_payload["reasoning_effort"] = Settings.REASONING_EFFORT

        url = self._get_completions_url()
        headers = self._get_auth_headers()

        full_text = ""
        token_count = 0
        start_time = time.time()

        try:
            # Stream from inference engine → relay tokens to Grid API
            async with self.backend.stream("POST", url, json=openai_payload, headers=headers) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    logger.error(f"Backend error {response.status_code}: {body[:200]}")
                    await self.ws.send(json.dumps({
                        "type": "done",
                        "id": job_id,
                        "full_text": "",
                    }))
                    return

                reasoning_open = False
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        # Reasoning models stream chain-of-thought in a separate
                        # field. Surface it LIVE, wrapped in <think>…</think>, so
                        # the UI renders a collapsible "thinking" section instead
                        # of showing nothing for ~60s before the answer.
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        content = delta.get("content") or ""

                        if reasoning:
                            if not reasoning_open:
                                reasoning_open = True
                                full_text += "<think>"
                                await self.ws.send(json.dumps({"type": "token", "id": job_id, "text": "<think>"}))
                            full_text += reasoning
                            token_count += 1
                            await self.ws.send(json.dumps({"type": "token", "id": job_id, "text": reasoning}))

                        if content:
                            if reasoning_open:
                                reasoning_open = False
                                full_text += "</think>\n\n"
                                await self.ws.send(json.dumps({"type": "token", "id": job_id, "text": "</think>\n\n"}))
                            full_text += content
                            token_count += 1
                            await self.ws.send(json.dumps({"type": "token", "id": job_id, "text": content}))
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue

                # Close an unterminated think block (model hit max_tokens mid-thought)
                if reasoning_open:
                    full_text += "</think>\n\n"
                    await self.ws.send(json.dumps({"type": "token", "id": job_id, "text": "</think>\n\n"}))

        except (httpx.ConnectError, httpx.ReadTimeout) as e:
            logger.error(f"Backend error during streaming: {e}")

        # Send completion
        gen_time = time.time() - start_time
        await self.ws.send(json.dumps({
            "type": "done",
            "id": job_id,
            "full_text": full_text,
        }))

        # Wait for ack with den reward
        try:
            ack = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
            den = ack.get("den", 0)
            self._jobs_completed += 1
            self._total_den += den

            tps = token_count / gen_time if gen_time > 0 else 0
            logger.info(
                f"✅ {job_id[:8]} | {token_count} tokens | {gen_time:.1f}s | "
                f"{tps:.1f} TPS | +{den:.1f} den | total: {self._total_den:.1f}"
            )
        except asyncio.TimeoutError:
            logger.warning(f"No ack received for job {job_id[:8]}")

    async def close(self):
        if self.ws:
            await self.ws.close()
        await self.backend.aclose()
