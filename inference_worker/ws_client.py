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
from datetime import datetime

import httpx
import websockets

from .config import Settings

logger = logging.getLogger(__name__)

BRIDGE_AGENT = "Grid Inference Worker:2.1.0:streaming"

# api_format -> backend endpoint suffix (appended to the OpenAI-compatible base).
FORMAT_SUFFIX = {
    "openai-chat": "chat/completions",
    "openai-responses": "responses",
    "anthropic": "messages",
}


def _safe_json(s: str):
    """Parse a JSON string, returning None on failure (for usage-tee best effort)."""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def _extract_usage(obj) -> dict | None:
    """Best-effort pull of a usage dict out of an Anthropic/Responses event body.

    Usage shows up in different places per format/event: top-level `usage`
    (responses non-stream, anthropic message_delta), `message.usage`
    (anthropic message_start), or `response.usage` (responses stream completed)."""
    if not isinstance(obj, dict):
        return None
    u = obj.get("usage")
    if not u and isinstance(obj.get("message"), dict):
        u = obj["message"].get("usage")
    if not u and isinstance(obj.get("response"), dict):
        u = obj["response"].get("usage")
    return u if isinstance(u, dict) else None


_SIGNER = None
_SIGNER_LOADED = False


def _load_or_create_signer():
    """Load (or generate) the worker's funds-LESS signing identity.

    A dedicated signing keypair (NOT the payout wallet — no funds ever live on
    the rig) used to sign per-job result receipts. From GRID_SIGNER_KEY, else
    persisted at ~/.aipg/worker_signer.key (0600), else generated once.
    Returns an eth_account Account, or None if eth-account isn't installed."""
    try:
        from eth_account import Account
    except ImportError:
        logger.warning("eth-account not installed — result receipts disabled (pip install eth-account)")
        return None
    import os
    try:
        pk = os.getenv("GRID_SIGNER_KEY", "").strip()
        if pk:
            return Account.from_key(pk)
        path = os.path.expanduser("~/.aipg/worker_signer.key")
        if os.path.exists(path):
            return Account.from_key(open(path).read().strip())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        acct = Account.create()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(acct.key.hex())
        logger.info(f"generated worker signing identity {acct.address} → {path}")
        return acct
    except Exception as e:
        logger.warning(f"signer init failed: {e}")
        return None


def get_signer():
    """Cached singleton — all parallel connections share one box identity."""
    global _SIGNER, _SIGNER_LOADED
    if not _SIGNER_LOADED:
        _SIGNER = _load_or_create_signer()
        _SIGNER_LOADED = True
    return _SIGNER


class BackendUnavailable(ConnectionError):
    """The local inference backend (vLLM/Ollama/LM Studio) is unreachable.

    Subclasses ConnectionError so the run() loop treats it like any other
    connection failure: close the WS (which deregisters us on the grid) and
    retry with backoff. The connect() health-gate then refuses to re-register
    until the backend is actually serving again — so a worker with a dead
    backend takes ITSELF offline instead of accepting jobs and returning
    empty results (the failure mode that silently halved the grid's chat
    throughput on 2026-06-14).
    """


class StreamingWorker:
    """WebSocket-based worker that streams tokens to the Grid API."""

    def __init__(self, name: str | None = None, spec=None):
        Settings.validate()
        # A worker serves ONE backend spec. `spec` carries the backend type/url/
        # key + model + advertised name; if omitted we build it from the classic
        # env vars (back-compat). Each parallel connection for a spec registers
        # under a distinct name (concurrency = N independent serial connections).
        if spec is None:
            from .config import load_backends
            spec = load_backends()[0]
        self.spec = spec
        self.name: str = name or spec.name
        self.model_name: str = spec.model_name
        self.grid_model_name: str = spec.grid_model_name or self._build_grid_model_name()
        self.backend = httpx.AsyncClient(timeout=120)
        self.ws = None
        self.worker_id = None
        self.api_formats = ["openai-chat"]
        self._signer = get_signer()
        self.signer_address = self._signer.address if self._signer else ""
        self._reconnect_delay = 1
        self._jobs_completed = 0
        self._total_den = 0.0
        self._job_in_flight = False

    def _sign_receipt(self, job_id: str, full_text: str):
        """Sign sha256(result) with the worker's identity key (EIP-191).

        Proves 'this worker produced this result for this job' — the grid stores
        the sig in the ledger for cryptographic attribution / future slashing.
        Returns None if no signer (eth-account absent) so the job still settles."""
        if not self._signer:
            return None
        try:
            import hashlib
            from eth_account.messages import encode_defunct
            result_hash = hashlib.sha256((full_text or "").encode()).hexdigest()
            signed = self._signer.sign_message(
                encode_defunct(text=f"aipg-receipt:v1:{job_id}:{result_hash}")
            )
            sig = signed.signature.hex()
            if not sig.startswith("0x"):
                sig = "0x" + sig
            return {"signer": self.signer_address, "sig": sig, "result_sha256": result_hash}
        except Exception as e:
            logger.warning(f"receipt sign failed for {job_id}: {e}")
            return None

    def _build_grid_model_name(self) -> str:
        return f"grid/{self.model_name}"

    def _endpoint_url(self, suffix: str) -> str:
        """Build a backend endpoint URL for an OpenAI-compatible path suffix."""
        if self.spec.backend_type == "ollama":
            return f"{self.spec.url.rstrip('/')}/v1/{suffix}"
        return f"{self.spec.url.rstrip('/')}/{suffix}"

    def _get_completions_url(self) -> str:
        return self._endpoint_url("chat/completions")

    async def _probe_formats(self) -> list:
        """Detect which API formats the backend natively serves.

        The grid routes /v1/messages and /v1/responses only to workers whose
        engine actually exposes them — so we probe each candidate endpoint and
        advertise only what answers. A 404 means the route doesn't exist (e.g.
        vLLM has no /v1/messages); any other status means it's there. We always
        keep openai-chat (our primary generation path)."""
        formats = ["openai-chat"]
        for fmt in ("openai-responses", "anthropic"):
            suffix = FORMAT_SUFFIX[fmt]
            try:
                r = await self.backend.post(
                    self._endpoint_url(suffix), json={}, headers=self._get_auth_headers(), timeout=5
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPError) as e:
                logger.info(f"Backend does not serve {fmt} ({suffix}): {type(e).__name__}")
                continue
            if r.status_code != 404:
                formats.append(fmt)
                logger.info(f"Backend serves {fmt} via /{suffix} (probe HTTP {r.status_code})")
            else:
                logger.info(f"Backend has no {fmt} endpoint (/{suffix} -> 404)")
        return formats

    def _get_auth_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.spec.backend_type != "ollama" and self.spec.api_key:
            headers["Authorization"] = f"Bearer {self.spec.api_key}"
        return headers

    def _health_url(self) -> str:
        """A cheap GET endpoint that proves the backend is actually serving."""
        if self.spec.backend_type == "ollama":
            return f"{self.spec.url.rstrip('/')}/api/tags"
        # OpenAI-compatible servers (vLLM/LM Studio/sglang) all expose /models
        return f"{self.spec.url.rstrip('/')}/models"

    async def _backend_healthy(self) -> bool:
        """True iff the local inference backend answers and serves our model.

        Used to gate registration and to confirm a backend has recovered. A
        worker must never advertise a model it can't actually run."""
        try:
            r = await self.backend.get(
                self._health_url(), headers=self._get_auth_headers(), timeout=5
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPError) as e:
            logger.warning(f"Backend health check failed ({type(e).__name__}): {self._health_url()}")
            return False
        if r.status_code != 200:
            logger.warning(f"Backend health check HTTP {r.status_code} from {self._health_url()}")
            return False
        return True

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
        # Health-gate: never register if the local backend isn't serving.
        # run() retries with backoff, so a worker whose vLLM/LM Studio is down
        # keeps probing and only comes online once it can actually generate.
        if not await self._backend_healthy():
            raise BackendUnavailable(
                f"Local backend not ready at {self._health_url()} — not registering"
            )

        # Detect which API formats the backend exposes so the grid can route
        # /v1/messages and /v1/responses to us only if we can actually serve them.
        self.api_formats = await self._probe_formats()

        logger.info(f"Connecting to {self.ws_url}... (formats: {self.api_formats})")

        self.ws = await websockets.connect(
            self.ws_url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        )

        # Send registration message
        await self.ws.send(json.dumps({
            "apikey": Settings.GRID_API_KEY,
            "name": self.name,
            "models": [self.grid_model_name],
            "max_length": Settings.MAX_LENGTH,
            "max_context_length": Settings.MAX_CONTEXT_LENGTH,
            "api_formats": self.api_formats,
            "signer_address": self.signer_address,
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
            monitor = None
            try:
                await self.connect()
                # Steady-state health: probe the backend periodically so a
                # backend that dies AFTER we registered takes us offline,
                # instead of accepting jobs until one fails mid-generation.
                monitor = asyncio.create_task(self._health_monitor())
                await self._message_loop()
            except BackendUnavailable as e:
                # Backend is down — we are deliberately OFF the grid (deregistered)
                # until it recovers. Keep probing with backoff; do not accept jobs.
                logger.warning(f"Backend offline ({e}); staying OFF the grid, re-checking in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30)
            except (websockets.ConnectionClosed, ConnectionRefusedError, ConnectionError, OSError) as e:
                logger.warning(f"Connection lost: {e}. Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30)
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                await asyncio.sleep(5)
            finally:
                if monitor:
                    monitor.cancel()

    async def _health_monitor(self):
        """Periodically re-probe the backend; close the WS (→ reconnect, which
        re-gates on health) if it goes unhealthy mid-connection. Skipped while a
        job is generating so a busy backend isn't mistaken for a dead one."""
        while True:
            await asyncio.sleep(30)
            if self._job_in_flight:
                continue
            if not await self._backend_healthy():
                logger.warning("Backend went unhealthy mid-connection — closing WS to deregister")
                try:
                    await self.ws.close()
                except Exception:
                    pass
                return

    async def _message_loop(self):
        """Process messages from the server."""
        while True:
            raw = await self.ws.recv()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "job":
                self._job_in_flight = True
                try:
                    await self._handle_job(msg)
                finally:
                    self._job_in_flight = False
            elif msg_type == "ping":
                await self.ws.send(json.dumps({"type": "pong"}))
            elif msg_type == "no_job":
                pass  # Server will push next job when available
            elif msg_type == "error":
                logger.error(f"Server error: {msg.get('message')}")
            else:
                logger.debug(f"Unknown message type: {msg_type}")

    async def _fail_job(self, job_id: str, message: str, client_error: bool = False):
        """Tell the grid this job failed.

        `client_error=True` marks a deterministic CALLER fault (bad request) so
        the grid surfaces the reason to the client and does NOT requeue or strike
        the worker — the same request would fail identically everywhere."""
        try:
            await self.ws.send(json.dumps({
                "type": "error", "id": job_id, "message": message, "client_error": client_error,
            }))
        except Exception:
            pass  # WS may already be gone; the grid's disconnect path requeues anyway

    def _build_backend_request(self, job_id: str, payload: dict) -> tuple[dict, bool]:
        """Build the OpenAI request we send to the local backend.

        Faithful passthrough: when the grid forwards a structured `request`
        (new protocol), we send it to the backend almost verbatim — same
        messages, tools, tool_choice, response_format, sampling params, etc. —
        overriding only what we MUST:

          * `model`     → our backend's actual model name
          * `stream`    → always True (we relay token-by-token)
          * `max_tokens`→ clamped to what this worker advertises, so a buggy or
                          malicious grid can't pin the GPU with a huge value
          * stream_options.include_usage → True, so the backend reports
                          authoritative token usage we forward to the grid

        Legacy grids send only a flattened `prompt`; we fall back to a minimal
        single-user-message request for them. Returns (request, faithful)."""
        request = payload.get("request")
        if isinstance(request, dict) and request.get("messages"):
            openai_payload = dict(request)
            openai_payload["model"] = self.model_name
            openai_payload["stream"] = True
            # Honor the client's requested output budget as-is — do NOT clamp it
            # down (that cut off long generations and starved tool-call loops).
            # Fall back to MAX_LENGTH only when the request didn't specify one.
            req_max = openai_payload.get("max_tokens") or payload.get("max_length")
            openai_payload["max_tokens"] = int(req_max) if req_max else Settings.MAX_LENGTH
            stream_opts = dict(openai_payload.get("stream_options") or {})
            stream_opts["include_usage"] = True
            openai_payload["stream_options"] = stream_opts
            faithful = True
        else:
            prompt = payload.get("prompt", "")
            max_tokens = int(payload.get("max_length") or Settings.MAX_LENGTH)
            openai_payload = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": float(payload.get("temperature", 0.7)),
                "top_p": float(payload.get("top_p", 0.9)),
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            faithful = False

        # Only apply a default reasoning_effort if the request didn't set one.
        if Settings.REASONING_EFFORT:
            openai_payload.setdefault("reasoning_effort", Settings.REASONING_EFFORT)
        return openai_payload, faithful

    async def _handle_job(self, job: dict):
        """Process a job: stream inference deltas back to the grid faithfully."""
        job_id = job["id"]
        model = job.get("model", self.model_name)
        payload = job.get("payload", {})

        # Raw passthrough formats (Anthropic /v1/messages, OpenAI /v1/responses)
        # are forwarded to the matching backend endpoint and relayed verbatim.
        api_format = payload.get("api_format", "openai-chat")
        if api_format != "openai-chat":
            await self._handle_raw_job(job_id, api_format, payload)
            return

        openai_payload, faithful = self._build_backend_request(job_id, payload)
        max_tokens = openai_payload.get("max_tokens")
        logger.info(
            f"📥 Job {job_id[:8]} | model={model} | max_tokens={max_tokens} | "
            f"mode={'passthrough' if faithful else 'legacy'}"
        )

        url = self._get_completions_url()
        headers = self._get_auth_headers()

        full_text = ""
        full_reasoning = ""
        token_count = 0
        usage = None
        last_finish = None
        start_time = time.time()

        try:
            # Stream from the inference engine → relay deltas to the grid.
            async with self.backend.stream("POST", url, json=openai_payload, headers=headers) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    detail = body.decode("utf-8", "replace")[:500]
                    logger.warning(f"Backend HTTP {response.status_code} for {job_id[:8]}: {detail[:200]}")
                    if 400 <= response.status_code < 500:
                        # CLIENT error (bad params / malformed tool schema / context
                        # too long). Now that we forward the request faithfully, the
                        # backend can reject it for reasons that are the CALLER's
                        # fault — not a backend-health problem. Fail just THIS job
                        # and surface the real reason; do NOT take ourselves offline
                        # (that would let one bad request knock the worker off the
                        # grid for everyone) and do NOT requeue (it would fail
                        # identically on every worker).
                        await self._fail_job(
                            job_id,
                            f"backend rejected request (HTTP {response.status_code}): {detail}",
                            client_error=True,
                        )
                        return
                    # 5xx / unexpected — a real backend health problem. Report a
                    # FAILURE so the grid requeues to a healthy worker + strikes us,
                    # then take ourselves offline until the backend recovers.
                    await self._fail_job(job_id, f"backend returned HTTP {response.status_code}")
                    raise BackendUnavailable(f"backend HTTP {response.status_code}")

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # The final usage frame (stream_options.include_usage) has
                    # empty choices — capture it and move on.
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    finish = choice.get("finish_reason")
                    if finish:
                        last_finish = finish

                    # Relay the RAW delta untouched, but only if it carries
                    # something meaningful — skip the bare {"role":"assistant"}
                    # opener (the grid emits its own single role chunk).
                    meaningful = any(
                        delta.get(k) for k in ("content", "reasoning_content", "tool_calls", "refusal")
                    )
                    if not meaningful:
                        continue

                    if delta.get("content"):
                        full_text += delta["content"]
                    if delta.get("reasoning_content"):
                        full_reasoning += delta["reasoning_content"]

                    token_count += 1
                    await self.ws.send(json.dumps({
                        "type": "token",
                        "id": job_id,
                        "delta": delta,
                        "finish_reason": finish,
                    }))

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            logger.error(f"Backend unreachable mid-generation: {e}")
            await self._fail_job(job_id, f"backend unreachable: {type(e).__name__}")
            raise BackendUnavailable(str(e)) from e

        # A 200 that yielded zero deltas is a silent backend failure (crashed on
        # load, empty stream). Report it as a failure so the grid retries on a
        # healthy worker instead of handing the client a blank reply.
        if token_count == 0 and not full_text and not full_reasoning:
            logger.error(f"Backend produced 0 tokens for job {job_id} — reporting failure")
            await self._fail_job(job_id, "backend produced no output")
            return

        # Send completion with the assembled text, reasoning, and authoritative usage.
        gen_time = time.time() - start_time
        done_msg = {
            "type": "done",
            "id": job_id,
            "full_text": full_text,
            "full_reasoning": full_reasoning,
            "finish_reason": last_finish or "stop",
        }
        if usage:
            done_msg["usage"] = usage
        receipt = self._sign_receipt(job_id, full_text)
        if receipt:
            done_msg["receipt"] = receipt  # cryptographic attribution of this result
        await self.ws.send(json.dumps(done_msg))

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

    async def _handle_raw_job(self, job_id: str, api_format: str, payload: dict):
        """Forward a raw Anthropic/Responses request to the backend and relay it.

        No mutation beyond overriding the model + clamping max tokens: the
        client's request goes to the native backend endpoint and the upstream
        events (SSE) or full JSON come back verbatim. Usage is teed for den."""
        suffix = FORMAT_SUFFIX.get(api_format)
        if not suffix:
            await self._fail_job(job_id, f"unsupported api_format '{api_format}'", client_error=True)
            return

        request = dict(payload.get("request") or {})
        request["model"] = self.model_name
        # Honor the client's output budget as-is (field name differs by API); the
        # backend's own context window is the real ceiling. We don't clamp it down.
        stream = bool(request.get("stream", False))
        url = self._endpoint_url(suffix)
        headers = self._get_auth_headers()
        start_time = time.time()
        usage = None

        logger.info(f"📥 Raw job {job_id[:8]} | format={api_format} | stream={stream}")

        try:
            if stream:
                cur_event = None
                async with self.backend.stream("POST", url, json=request, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        detail = body.decode("utf-8", "replace")[:500]
                        if 400 <= resp.status_code < 500:
                            await self._fail_job(
                                job_id, f"backend rejected request (HTTP {resp.status_code}): {detail}",
                                client_error=True,
                            )
                            return
                        await self._fail_job(job_id, f"backend returned HTTP {resp.status_code}")
                        raise BackendUnavailable(f"backend HTTP {resp.status_code}")

                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("event:"):
                            cur_event = line[6:].strip()
                            continue
                        if line.startswith("data:"):
                            data = line[5:].strip()
                            if data == "[DONE]":
                                continue
                            u = _extract_usage(_safe_json(data))
                            if u:
                                usage = {**(usage or {}), **u}
                            await self.ws.send(json.dumps(
                                {"type": "raw", "id": job_id, "event": cur_event, "data": data}
                            ))
                            cur_event = None
                await self.ws.send(json.dumps({"type": "done", "id": job_id, "usage": usage}))
            else:
                resp = await self.backend.post(url, json=request, headers=headers)
                if resp.status_code != 200:
                    detail = resp.text[:500]
                    if 400 <= resp.status_code < 500:
                        await self._fail_job(
                            job_id, f"backend rejected request (HTTP {resp.status_code}): {detail}",
                            client_error=True,
                        )
                        return
                    await self._fail_job(job_id, f"backend returned HTTP {resp.status_code}")
                    raise BackendUnavailable(f"backend HTTP {resp.status_code}")
                obj = resp.json()
                usage = _extract_usage(obj)
                await self.ws.send(json.dumps({"type": "done", "id": job_id, "full_json": obj, "usage": usage}))

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            logger.error(f"Backend unreachable mid-raw-generation: {e}")
            await self._fail_job(job_id, f"backend unreachable: {type(e).__name__}")
            raise BackendUnavailable(str(e)) from e

        # Wait for the den ack.
        try:
            ack = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
            den = ack.get("den", 0)
            self._jobs_completed += 1
            self._total_den += den
            gen_time = time.time() - start_time
            out = (usage or {}).get("output_tokens") or (usage or {}).get("completion_tokens") or 0
            logger.info(
                f"✅ {job_id[:8]} | {api_format} | {out} out-tok | {gen_time:.1f}s | "
                f"+{den:.1f} den | total: {self._total_den:.1f}"
            )
        except asyncio.TimeoutError:
            logger.warning(f"No ack received for raw job {job_id[:8]}")

    async def close(self):
        if self.ws:
            await self.ws.close()
        await self.backend.aclose()


# ── Concurrency throttle + optional schedule ──────────────────────────────
# Concurrency = how many parallel grid connections this box runs, each one
# in-flight job. Keep GRID_MAX_THREADS below the backend's comfortable batch
# size so a local app sharing the same vLLM keeps headroom. GRID_SCHEDULE
# optionally varies it by time of day (operator local time); concurrency 0 in a
# window = paused (fully off the grid, all capacity left to the local app).

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_days(spec):
    """'mon-fri' / 'sat,sun' / 'mon' / '' (all) -> set of weekday ints (Mon=0)."""
    spec = (spec or "").strip().lower()
    if not spec or spec in ("*", "all", "daily"):
        return set(range(7))
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (x.strip() for x in part.split("-", 1))
            ai, bi = _DAYS.get(a), _DAYS.get(b)
            if ai is None or bi is None:
                continue
            i = ai
            while True:
                out.add(i)
                if i == bi:
                    break
                i = (i + 1) % 7
        elif part in _DAYS:
            out.add(_DAYS[part])
    return out or set(range(7))


def _hm(s):
    """'HH:MM' -> minutes since midnight, or None."""
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _window_active(now, w) -> bool:
    if now.weekday() not in _parse_days(w.get("days")):
        return False
    start = _hm(w.get("start", "00:00")) or 0
    end = _hm(w["end"]) if w.get("end") else 24 * 60
    cur = now.hour * 60 + now.minute
    return start <= cur < end if start <= end else (cur >= start or cur < end)


def effective_concurrency(backend=None, now=None) -> int:
    """Concurrency for 'now' per the backend's schedule; else its concurrency.
    Clamped >=0. With no backend, falls back to the global env (back-compat)."""
    if backend is not None:
        base = max(int(backend.concurrency), 0)
        raw = (backend.schedule or "").strip()
    else:
        base = max(int(Settings.MAX_THREADS), 0)
        raw = (Settings.GRID_SCHEDULE or "").strip()
    if not raw:
        return base
    now = now or datetime.now()
    try:
        for w in json.loads(raw):
            if _window_active(now, w):
                return max(int(w.get("concurrency", base)), 0)
    except Exception as e:
        logger.warning(f"GRID_SCHEDULE parse error ({e}); falling back to MAX_THREADS={base}")
    return base


SUPERVISOR_INTERVAL = 30  # seconds between concurrency re-evaluations


def _connection_names(base: str, n: int):
    if n <= 0:
        return []
    # Slot 1 keeps the plain base name (back-compatible with single-worker
    # setups); extra slots are base#2..#N.
    return [base] + [f"{base}#{i}" for i in range(2, n + 1)]


async def _run_one(name: str, spec):
    """Run one connection forever (its own reconnect loop), cleaning up on cancel."""
    w = StreamingWorker(name=name, spec=spec)
    try:
        await w.run()
    finally:
        try:
            await w.close()
        except Exception:
            pass


async def run_workers():
    """Supervisor: across ALL configured backends, keep each backend's
    `effective_concurrency()` parallel connections alive, scaling with schedules.
    One binary, many (backend, model) pairs. concurrency 0 = that backend paused."""
    from .config import load_backends
    backends = load_backends()
    logger.info(
        "serving %d backend(s): %s",
        len(backends),
        ", ".join(f"{b.grid_model_name}(x{b.concurrency})" for b in backends),
    )
    tasks: dict[str, asyncio.Task] = {}
    specs: dict[str, object] = {}  # connection name -> backend spec
    last: dict[str, int] = {}
    try:
        while True:
            want: set[str] = set()
            for b in backends:
                n = effective_concurrency(b)
                names = _connection_names(b.name, n)
                want.update(names)
                for nm in names:
                    specs[nm] = b
                if n != last.get(b.name):
                    logger.info(f"[{b.grid_model_name}] concurrency={n} → {sorted(names) or 'PAUSED'}")
                    last[b.name] = n
            for nm in want:  # spawn missing / restart any that died
                t = tasks.get(nm)
                if t is None or t.done():
                    tasks[nm] = asyncio.create_task(_run_one(nm, specs[nm]))
            for nm in list(tasks):  # cancel extras (scaled down / paused)
                if nm not in want:
                    tasks[nm].cancel()
                    tasks.pop(nm, None)
            await asyncio.sleep(SUPERVISOR_INTERVAL)
    finally:
        for t in tasks.values():
            t.cancel()
