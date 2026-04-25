# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""P2P client for decentralized Grid workers.

Uses libp2p to:
1. Join the P2P mesh via gossipsub
2. Subscribe to job topics for configured model
3. Claim jobs using deterministic selection
4. Stream results back via DIRECT CONNECTION to requester

Enable with P2P_ENABLED=true in your .env.

This worker runs trio directly (not asyncio) since it's standalone.

Architecture:
- Gossipsub: job broadcasts, claim broadcasts (one-to-many)
- Direct streams: result streaming to requester (one-to-one, efficient)
"""

# Protocol ID for direct result streaming
RESULT_STREAM_PROTOCOL = "/aipg/1/result-stream"

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import trio

from .config import Settings

logger = logging.getLogger(__name__)

# Topic structure
TOPIC_PREFIX = "/aipg/1"


def job_topic(model: str) -> str:
    """Get gossipsub topic for jobs targeting a model."""
    safe_model = model.replace("/", "-").replace(":", "-")
    return f"{TOPIC_PREFIX}/jobs/{safe_model}"


def claims_topic() -> str:
    """Get the global claims topic."""
    return f"{TOPIC_PREFIX}/claims"


def results_topic(job_id: str) -> str:
    """Get topic for a specific job's results."""
    return f"{TOPIC_PREFIX}/results/{job_id}"


def _strip_thinking_tags(text: str) -> str:
    """Remove think-tag blocks from reasoning models."""
    if not text:
        return text
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL).strip()


@dataclass
class JobRequest:
    """A job received from the P2P network."""
    id: str
    model: str
    payload: dict[str, Any]
    max_cost: int
    user_pubkey: str
    signature: str
    timestamp: float
    requester_peer_id: str = ""  # Peer ID to stream results to
    ttl: int = 60

    @classmethod
    def from_json(cls, data: str) -> "JobRequest":
        d = json.loads(data)
        # Handle missing requester_peer_id for backwards compatibility
        if "requester_peer_id" not in d:
            d["requester_peer_id"] = ""
        return cls(**d)

    def is_expired(self) -> bool:
        return time.time() > self.timestamp + self.ttl

    def seed(self) -> bytes:
        """Get random seed for claim resolution."""
        return bytes.fromhex(self.signature[:64])


@dataclass
class JobClaim:
    """A claim broadcast to prevent double-processing."""
    job_id: str
    worker_id: str
    worker_pubkey: str
    price: int
    signature: str
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, data: str) -> "JobClaim":
        return cls(**json.loads(data))


def compute_claim_score(job_id: str, seed: bytes, worker_id: str) -> bytes:
    """Compute deterministic score for claim resolution. Lower wins."""
    data = job_id.encode() + seed + worker_id.encode()
    return hashlib.sha256(data).digest()


def should_claim(job: JobRequest, my_worker_id: str, known_workers: list[str]) -> bool:
    """Determine if this worker should claim the job."""
    if not known_workers:
        return True

    seed = job.seed()
    my_score = compute_claim_score(job.id, seed, my_worker_id)

    for worker_id in known_workers:
        if worker_id == my_worker_id:
            continue
        their_score = compute_claim_score(job.id, seed, worker_id)
        if their_score < my_score:
            return False

    return True


class P2PWorker:
    """P2P-based worker using libp2p gossipsub.

    Runs with trio event loop (not asyncio).
    """

    def __init__(self):
        Settings.validate()
        self.model_name: str = Settings.MODEL_NAME
        self.grid_model_name: str = Settings.GRID_MODEL_NAME or f"grid/{self.model_name}"

        # P2P state
        self._host = None
        self._pubsub = None
        self._gossipsub = None
        self.peer_id: str = ""
        self._known_workers: set[str] = set()
        self._claimed_jobs: dict[str, JobClaim] = {}
        self._running = False

        # Subscriptions
        self._job_subscription = None
        self._claims_subscription = None

        # Stats
        self._jobs_completed = 0

    def _get_completions_url(self) -> str:
        if Settings.BACKEND_TYPE == "ollama":
            return f"{Settings.OLLAMA_URL}/v1/chat/completions"
        return f"{Settings.OPENAI_URL}/chat/completions"

    def _get_auth_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if Settings.BACKEND_TYPE != "ollama" and Settings.OPENAI_API_KEY:
            headers["Authorization"] = f"Bearer {Settings.OPENAI_API_KEY}"
        return headers

    async def run(self) -> None:
        """Main entry point - runs with trio."""
        from libp2p import new_host
        from libp2p.crypto.secp256k1 import create_new_key_pair
        from libp2p.peer.peerinfo import info_from_p2p_addr
        from libp2p.pubsub.gossipsub import GossipSub
        from libp2p.pubsub.pubsub import Pubsub
        from libp2p.stream_muxer.mplex.mplex import MPLEX_PROTOCOL_ID, Mplex
        from libp2p.tools.anyio_service import background_trio_service
        from libp2p.custom_types import TProtocol
        from libp2p.peer.id import ID
        import multiaddr

        # Generate key pair
        key_pair = create_new_key_pair()

        # Create host
        self._host = new_host(
            key_pair=key_pair,
            muxer_opt={MPLEX_PROTOCOL_ID: Mplex},
        )

        # Create gossipsub
        self._gossipsub = GossipSub(
            protocols=[TProtocol("/meshsub/1.0.0")],
            degree=6,
            degree_low=4,
            degree_high=12,
            time_to_live=5,
            heartbeat_interval=5,
        )
        self._pubsub = Pubsub(self._host, self._gossipsub)

        # Listen address
        listen_port = Settings.P2P_LISTEN_PORT
        listen_addrs = [f"/ip4/0.0.0.0/tcp/{listen_port}"]

        async with self._host.run(listen_addrs=listen_addrs), trio.open_nursery() as nursery:
            # Start peerstore cleanup
            nursery.start_soon(self._host.get_peerstore().start_cleanup_task, 60)

            async with background_trio_service(self._pubsub):
                async with background_trio_service(self._gossipsub):
                    await self._pubsub.wait_until_ready()

                    # Get peer ID
                    self.peer_id = self._host.get_id().to_string()
                    self._known_workers.add(self.peer_id)
                    self._running = True

                    logger.info(f"🚀 P2P Worker started | model={self.grid_model_name}")
                    logger.info(f"📡 Backend: {Settings.BACKEND_TYPE} @ {self._get_completions_url()}")
                    logger.info(f"🔗 Peer ID: {self.peer_id}")
                    logger.info(f"🎧 Listening on port {listen_port}")

                    # Connect to bootstrap peers
                    for peer_addr in Settings.P2P_BOOTSTRAP_PEERS:
                        try:
                            maddr = multiaddr.Multiaddr(peer_addr)
                            info = info_from_p2p_addr(maddr)
                            await self._host.connect(info)
                            logger.info(f"✅ Connected to bootstrap peer: {info.peer_id}")
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to connect to {peer_addr}: {e}")

                    # Subscribe to job topic
                    topic = job_topic(self.grid_model_name)
                    self._job_subscription = await self._pubsub.subscribe(topic)
                    logger.info(f"📥 Subscribed to {topic}")

                    # Subscribe to claims topic
                    self._claims_subscription = await self._pubsub.subscribe(claims_topic())
                    logger.info(f"📥 Subscribed to {claims_topic()}")

                    # Start message handlers
                    nursery.start_soon(self._job_loop, ID)
                    nursery.start_soon(self._claims_loop, ID)

                    # Run until cancelled
                    logger.info("⏳ Waiting for jobs...")
                    await trio.sleep_forever()

    async def _job_loop(self, ID) -> None:
        """Process incoming job messages."""
        while self._running:
            try:
                message = await self._job_subscription.get()
                from_peer = ID(message.from_id).to_base58()

                # Skip our own messages
                if from_peer == self.peer_id:
                    continue

                data = message.data.decode()
                job = JobRequest.from_json(data)

                # Skip expired
                if job.is_expired():
                    continue

                # Skip already claimed
                if job.id in self._claimed_jobs:
                    claim = self._claimed_jobs[job.id]
                    if claim.worker_id != self.peer_id:
                        continue

                # Check if we should claim
                if not should_claim(job, self.peer_id, list(self._known_workers)):
                    logger.debug(f"Not our turn for job {job.id[:8]}")
                    continue

                # Process the job
                await self._handle_job(job)

            except Exception as e:
                if "cancelled" in str(e).lower():
                    break
                logger.error(f"Job loop error: {e}")
                await trio.sleep(0.1)

    async def _claims_loop(self, ID) -> None:
        """Process incoming claim messages."""
        while self._running:
            try:
                message = await self._claims_subscription.get()
                from_peer = ID(message.from_id).to_base58()

                # Skip our own messages
                if from_peer == self.peer_id:
                    continue

                data = message.data.decode()
                claim = JobClaim.from_json(data)

                # Record claim (first wins)
                existing = self._claimed_jobs.get(claim.job_id)
                if not existing or claim.timestamp < existing.timestamp:
                    self._claimed_jobs[claim.job_id] = claim
                    self._known_workers.add(claim.worker_id)
                    logger.debug(f"Recorded claim: {claim.worker_id[:8]} -> {claim.job_id[:8]}")

            except Exception as e:
                if "cancelled" in str(e).lower():
                    break
                logger.error(f"Claims loop error: {e}")
                await trio.sleep(0.1)

    async def _handle_job(self, job: JobRequest) -> None:
        """Process a single job.

        Opens a direct stream to the requester and streams results over it.
        More efficient than gossipsub for high-frequency token streaming.
        """
        from libp2p.peer.id import ID
        from libp2p.custom_types import TProtocol
        import multiaddr

        job_id = job.id
        payload = job.payload
        requester_peer_id = job.requester_peer_id

        # Broadcast our claim via gossipsub
        claim = JobClaim(
            job_id=job_id,
            worker_id=self.peer_id,
            worker_pubkey="",
            price=0,
            signature="",
        )
        self._claimed_jobs[job_id] = claim
        await self._pubsub.publish(claims_topic(), claim.to_json().encode())
        logger.info(f"📋 Claimed job {job_id[:8]}")

        # Extract prompt - handle both formats
        if "messages" in payload:
            messages = payload["messages"]
        elif "prompt" in payload:
            messages = [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": payload["prompt"]},
            ]
        else:
            logger.error(f"Unknown payload format for job {job_id[:8]}")
            return

        max_tokens = int(payload.get("max_length", payload.get("max_tokens", 512)))
        temperature = float(payload.get("temperature", 0.7))
        top_p = float(payload.get("top_p", 0.9))

        logger.info(f"📥 Processing {job_id[:8]} | max_tokens={max_tokens}")

        # Open direct stream to requester
        stream = None
        if requester_peer_id:
            try:
                peer_id = ID.from_base58(requester_peer_id)
                stream = await self._host.new_stream(
                    peer_id, [TProtocol(RESULT_STREAM_PROTOCOL)]
                )
                # Send job_id as first line
                await stream.write(f"{job_id}\n".encode())
                logger.debug(f"Opened result stream to {requester_peer_id[:8]}")
            except Exception as e:
                logger.warning(f"Failed to open stream to requester: {e}")
                stream = None

        # Build OpenAI-compatible request
        openai_payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,
        }

        url = self._get_completions_url()
        headers = self._get_auth_headers()

        full_text = ""
        token_count = 0
        start_time = time.time()

        async def send_result(result_dict: dict) -> None:
            """Send result via stream or fallback to gossipsub."""
            data = json.dumps(result_dict).encode() + b"\n"
            if stream:
                try:
                    await stream.write(data)
                except Exception as e:
                    logger.warning(f"Stream write failed: {e}")
            else:
                # Fallback to gossipsub (less efficient)
                await self._pubsub.publish(results_topic(job_id), data)

        # Use httpx with trio backend
        async with httpx.AsyncClient(timeout=120) as client:
            try:
                async with client.stream("POST", url, json=openai_payload, headers=headers) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        logger.error(f"Backend error {response.status_code}: {body[:200]}")
                        await send_result({
                            "job_id": job_id,
                            "worker_id": self.peer_id,
                            "type": "error",
                            "error": {"message": f"Backend error: {response.status_code}", "code": response.status_code},
                        })
                        if stream:
                            await stream.close()
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                content = _strip_thinking_tags(content) if "<think" in content else content
                                if content:
                                    full_text += content
                                    token_count += 1

                                    # Stream token via direct connection
                                    await send_result({
                                        "job_id": job_id,
                                        "worker_id": self.peer_id,
                                        "type": "token",
                                        "token": {"text": content, "index": token_count},
                                    })
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue

            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                logger.error(f"Backend error: {e}")
                await send_result({
                    "job_id": job_id,
                    "worker_id": self.peer_id,
                    "type": "error",
                    "error": {"message": str(e), "code": 0},
                })
                if stream:
                    await stream.close()
                return

        # Send completion
        gen_time = time.time() - start_time
        await send_result({
            "job_id": job_id,
            "worker_id": self.peer_id,
            "type": "done",
            "done": {
                "full_text": full_text,
                "token_count": token_count,
                "receipt_signature": "",
            },
        })

        # Close the stream
        if stream:
            try:
                await stream.close()
            except:
                pass

        self._jobs_completed += 1
        tps = token_count / gen_time if gen_time > 0 else 0
        stream_type = "direct" if stream else "gossipsub"
        logger.info(
            f"✅ {job_id[:8]} | {token_count} tokens | {gen_time:.1f}s | "
            f"{tps:.1f} TPS | {stream_type} | total: {self._jobs_completed}"
        )

        # Cleanup old claims periodically
        if self._jobs_completed % 10 == 0:
            self._cleanup_claims()

    def _cleanup_claims(self) -> None:
        """Remove old claims to prevent memory leak."""
        now = time.time()
        ttl = 120  # Keep claims for 2 minutes

        expired = [
            job_id for job_id, claim in self._claimed_jobs.items()
            if now - claim.timestamp > ttl
        ]
        for job_id in expired:
            del self._claimed_jobs[job_id]

        if expired:
            logger.debug(f"Cleaned up {len(expired)} old claims")

    async def stop(self) -> None:
        """Stop the worker."""
        self._running = False
        logger.info("P2P worker stopped")


def run_p2p_worker() -> None:
    """Entry point for P2P worker mode."""
    worker = P2PWorker()
    try:
        trio.run(worker.run)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
