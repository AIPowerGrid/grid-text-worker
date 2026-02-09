"""Text inference worker — bridges between AI Power Grid and an Ollama/OpenAI backend."""

import asyncio
import logging
import time
from collections import deque

import httpx

from .api_client import APIClient
from .config import Settings

logger = logging.getLogger(__name__)

# AIPG context injected when prompt mentions the project
AIPG_CONTEXT = """AI Power Grid (AIPG) is a distributed network for AI workloads with native cryptocurrency incentives. Key points:

- Platform: Distributed AI compute network built on AI Horde with workflow engine
- Tokenomics: 150M max supply
- Network: P2P port 8865, RPC port 9788, PoW/PoUW consensus
- Links: aipowergrid.io, explorer.aipowergrid.io, pool.aipowergrid.io
- Social: @AIPowerGrid (Twitter), t.me/AIPowerGrid (Telegram)
- Meet founder: https://calendly.com/half-aipowergrid/30min"""

AIPG_TERMS = ["aipg", "ai power grid", "aipowergrid"]


class WorkerStats:
    """Track kudos/hr and jobs/hr using a sliding window."""

    def __init__(self):
        self.kudos_record = deque()
        self.jobs_completed = 0
        self.jobs_failed = 0
        self.total_tokens = 0
        self.total_kudos = 0
        self.last_job_time = None
        self.last_job_kudos = 0
        self.start_time = time.time()

    def record_job(self, kudos: float, tokens: int = 0):
        now = time.time()
        self.kudos_record.append((kudos, now))
        self.jobs_completed += 1
        self.total_tokens += tokens
        self.total_kudos += kudos
        self.last_job_time = now
        self.last_job_kudos = kudos
        # Prune older than 1 hour
        cutoff = now - 3600
        while self.kudos_record and self.kudos_record[0][1] < cutoff:
            self.kudos_record.popleft()

    def record_failure(self):
        self.jobs_failed += 1

    @property
    def kudos_per_hour(self) -> float:
        if len(self.kudos_record) < 2:
            return 0
        now = time.time()
        oldest = self.kudos_record[0][1]
        period = now - oldest
        if period < 10:
            return 0
        total = sum(k for k, _ in self.kudos_record)
        return total * (3600 / period)

    @property
    def jobs_per_hour(self) -> float:
        if len(self.kudos_record) < 2:
            return 0
        now = time.time()
        oldest = self.kudos_record[0][1]
        period = now - oldest
        if period < 10:
            return 0
        return len(self.kudos_record) * (3600 / period)

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    def format_since_last(self) -> str:
        if not self.last_job_time:
            return ""
        elapsed = time.time() - self.last_job_time
        if elapsed < 60:
            return f"{int(elapsed)}s ago"
        if elapsed < 3600:
            return f"{int(elapsed/60)}m ago"
        return f"{int(elapsed/3600)}h {int((elapsed%3600)/60)}m ago"

    def to_dict(self) -> dict:
        """Expose stats for the web dashboard."""
        return {
            "jobs_completed": self.jobs_completed,
            "jobs_failed": self.jobs_failed,
            "total_tokens": self.total_tokens,
            "total_kudos": self.total_kudos,
            "kudos_per_hour": round(self.kudos_per_hour, 1),
            "jobs_per_hour": round(self.jobs_per_hour, 1),
            "last_job_kudos": self.last_job_kudos,
            "last_job_time": self.last_job_time,
            "uptime_seconds": round(self.uptime_seconds),
        }


def _fmt_num(n: float) -> str:
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    if n >= 1_000:
        return f"{n/1e3:.1f}K"
    return str(int(n))


def _trunc(s: str, maxlen: int = 15) -> str:
    return s[:maxlen-2] + ".." if len(s) > maxlen else s


class TextWorker:
    def __init__(self):
        self.api = APIClient()
        self.backend = httpx.AsyncClient(timeout=120)
        self.model_name: str = Settings.MODEL_NAME
        self.grid_model_name: str = Settings.GRID_MODEL_NAME or self._build_grid_model_name()
        self.stats = WorkerStats()
        self.consecutive_failures = 0
        self._last_status_log = 0
        self._pop_failures = 0

    def _build_grid_model_name(self) -> str:
        """Build the grid-advertised model name with domain prefix."""
        if Settings.BACKEND_TYPE == "ollama":
            return f"grid/{self.model_name}"
        url = Settings.OPENAI_URL.lower()
        if "openai.com" in url:
            return f"openai/{self.model_name}"
        return f"grid/{self.model_name}"

    def _get_completions_url(self) -> str:
        if Settings.BACKEND_TYPE == "ollama":
            return f"{Settings.OLLAMA_URL}/v1/chat/completions"
        return f"{Settings.OPENAI_URL}/chat/completions"

    def _get_auth_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if Settings.BACKEND_TYPE == "ollama":
            return headers
        if Settings.OPENAI_API_KEY:
            headers["Authorization"] = f"Bearer {Settings.OPENAI_API_KEY}"
        return headers

    def _stale_timeout(self, max_tokens: int) -> float:
        """Calculate max allowed generation time before a job is stale."""
        return (max_tokens / 2) + 10

    def _transform_payload(self, payload: dict) -> dict:
        prompt = payload.get("prompt", "")
        max_tokens = int(payload.get("max_length", 80))
        temperature = float(payload.get("temperature", 0.8))
        top_p = float(payload.get("top_p", 0.9))

        has_aipg = any(term in prompt.lower() for term in AIPG_TERMS)
        if has_aipg:
            system_prompt = "You are a helpful assistant with expertise in AI Power Grid (AIPG). Provide concise, accurate information about the platform."
            prompt = f"{AIPG_CONTEXT}\n\nUser Query: {prompt}"
        else:
            system_prompt = "You are a helpful assistant."

        openai_payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }

        if "stop_sequence" in payload:
            openai_payload["stop"] = payload["stop_sequence"]
        if "frequency_penalty" in payload:
            openai_payload["frequency_penalty"] = float(payload["frequency_penalty"])
        if "presence_penalty" in payload:
            openai_payload["presence_penalty"] = float(payload["presence_penalty"])

        return openai_payload

    def _log_waiting(self):
        """Log a compact waiting status line every 5 seconds."""
        now = time.time()
        if now - self._last_status_log < 5:
            return
        self._last_status_log = now

        status_msg = f"{'⏳ Waiting for jobs..':<20}"
        thread_msg = f"👥 Threads: {Settings.MAX_THREADS}/{Settings.MAX_THREADS}"
        thread_col = f"{thread_msg:<16}"

        parts = [status_msg, f"| {thread_col}"]

        if self.stats.kudos_per_hour > 0:
            kph = _fmt_num(self.stats.kudos_per_hour)
            parts.append(f"| 🌟{kph:<6} 電/hr")
        if self.stats.jobs_per_hour > 0:
            jph = _fmt_num(self.stats.jobs_per_hour)
            parts.append(f"| 🔄 {jph:<6} jobs/hr")
        if self.stats.last_job_time:
            parts.append(f"| ⏱️ Last job: {self.stats.format_since_last()}")

        logger.info("".join(parts))

    def _log_received(self, job_id: str, tokens: int):
        model_name = _trunc(self.model_name)
        job_col = f"{'✅ Received ' + job_id[:8]:<20}"
        model_col = f"{'🧠 ' + model_name:<16}"
        token_col = f"{'📊' + str(tokens) + ' tokens':<16}"
        logger.info(f"{job_col}| {model_col}| {token_col}| 🆕 Job")

    def _log_completed(self, job_id: str, tokens: int, gen_time: float, kudos: float):
        model_name = _trunc(self.model_name)
        tps = tokens / gen_time if gen_time > 0 else 0
        if tps >= 10:
            speed = "🐇Fast"
        elif tps >= 5:
            speed = "🚶Moderate"
        else:
            speed = "🐢Slow"

        job_col = f"{'✅ Complete ' + job_id[:8]:<20}"
        model_col = f"{'🧠 ' + model_name:<16}"
        speed_col = f"{speed:<16}"
        tps_col = f"⚡  {tps:<7.1f}TPS"
        logger.info(f"{job_col}| {model_col}| {speed_col}| {tps_col}")

    async def process_once(self) -> bool:
        """Pop one job, run inference, submit result."""
        # Pop with resilience
        try:
            job = await self.api.pop_job([self.grid_model_name])
            self._pop_failures = 0
        except httpx.ConnectError:
            self._pop_failures += 1
            wait = min(10, 2 * self._pop_failures)
            logger.warning(f"Server {Settings.GRID_API_URL} unavailable during pop. Waiting {wait} seconds...")
            await asyncio.sleep(wait)
            return False
        except httpx.ReadTimeout:
            logger.warning(f"Server {Settings.GRID_API_URL} timed out during pop. Waiting 2 seconds...")
            await asyncio.sleep(2)
            return False
        except Exception as e:
            self._pop_failures += 1
            logger.error(f"Pop error: {e}")
            await asyncio.sleep(5)
            return False

        if not job:
            self._log_waiting()
            return False

        job_id = job["id"]
        payload = job.get("payload", {})
        max_tokens = int(payload.get("max_length", 80))
        self._log_received(job_id, max_tokens)

        # Transform and send to backend
        openai_payload = self._transform_payload(payload)
        url = self._get_completions_url()
        headers = self._get_auth_headers()
        stale_timeout = self._stale_timeout(max_tokens)

        text = ""
        faulted = False
        retries = 0
        start_time = time.time()
        while retries < 5:
            # Stale detection — abort if total time exceeds threshold
            elapsed = time.time() - start_time
            if elapsed > stale_timeout:
                logger.warning(f"⏱️ Job is stale after {elapsed:.1f}s — aborting")
                break

            try:
                resp = await self.backend.post(url, json=openai_payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices and "message" in choices[0]:
                        text = choices[0]["message"].get("content", "")
                    break
                elif resp.status_code == 422:
                    logger.error(f"Backend validation error. Aborting.")
                    faulted = True
                    break
                elif resp.status_code == 429:
                    logger.warning(f"Rate limit exceeded. Retrying in 5 seconds...")
                    await asyncio.sleep(5)
                    retries += 1
                elif resp.status_code >= 500:
                    logger.warning(f"Server error from backend. Retrying in 3 seconds...")
                    await asyncio.sleep(3)
                    retries += 1
                else:
                    logger.error(f"Backend error {resp.status_code}: {resp.text[:80]}")
                    faulted = True
                    break
            except httpx.ConnectError:
                logger.error(f"Backend connection error. Retrying in 3 seconds... (attempt {retries + 1}/5)")
                await asyncio.sleep(3)
                retries += 1
            except httpx.ReadTimeout:
                logger.error(f"Backend request timeout. Retrying in 3 seconds... (attempt {retries + 1}/5)")
                await asyncio.sleep(3)
                retries += 1
        gen_time = time.time() - start_time

        # Always submit — even empty string — so the job doesn't hang in the grid
        submit_payload = {
            "id": job_id,
            "generation": text,
            "seed": 0,
        }
        if faulted:
            submit_payload["state"] = "faulted"

        kudos = 0
        model_name = _trunc(self.model_name)
        try:
            result = await self.api.submit_result(submit_payload)
            kudos = result.get("reward", 0) if isinstance(result, dict) else 0
            if faulted or not text:
                self.consecutive_failures += 1
                self.stats.record_failure()
                err_col = f"{'❌ Failed ' + job_id[:8]:<20}"
                model_col = f"{'🧠 ' + model_name:<16}"
                logger.warning(f"{err_col}| {model_col}| Error")
            else:
                self.consecutive_failures = 0
                self.stats.record_job(kudos, max_tokens)
                self._log_completed(job_id, max_tokens, gen_time, kudos)
        except Exception as e:
            self.consecutive_failures += 1
            self.stats.record_failure()
            err_col = f"{'❌ Failed ' + job_id[:8]:<20}"
            model_col = f"{'🧠 ' + model_name:<16}"
            logger.error(f"{err_col}| {model_col}| {e}")

        # Too many consecutive failures — back off
        if self.consecutive_failures >= 5:
            logger.error("⚠️ Too many consecutive failures. Backing off 30 seconds...")
            await asyncio.sleep(30)
            self.consecutive_failures = 0

        return True

    async def run(self):
        """Main worker loop."""
        init = f"{'🚀 Worker starting':<20}"
        model = f"🧠 {self.grid_model_name}"
        logger.info(f"{init}| {model}")
        logger.info(f"{'📡 Backend':<20}| {Settings.BACKEND_TYPE} @ {self._get_completions_url()}")

        while True:
            try:
                processed = await self.process_once()
                if not processed:
                    await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Error in worker loop: {e}")
                await asyncio.sleep(5)

    async def cleanup(self):
        await self.backend.aclose()
        await self.api.close()
