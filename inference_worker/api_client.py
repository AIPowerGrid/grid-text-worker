import asyncio
import logging
from typing import List, Any, Dict, Optional

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

BRIDGE_VERSION = "1.0.0"
BRIDGE_AGENT = f"AI Horde Worker:{BRIDGE_VERSION}:https://github.com/AIPowerGrid/text-worker-bridge"


class APIClient:
    def __init__(self):
        Settings.validate()
        self.client = httpx.AsyncClient(base_url=Settings.GRID_API_URL, timeout=60)
        self.headers = {
            "apikey": Settings.GRID_API_KEY,
            "Content-Type": "application/json",
        }

    async def pop_job(self, models: List[str]) -> Optional[Dict[str, Any]]:
        """Pop a text generation job from the grid."""
        payload = {
            "name": Settings.GRID_WORKER_NAME,
            "models": models,
            "max_length": Settings.MAX_LENGTH,
            "max_context_length": Settings.MAX_CONTEXT_LENGTH,
            "priority_usernames": [],
            "threads": Settings.MAX_THREADS,
            "bridge_agent": BRIDGE_AGENT,
        }
        if Settings.WALLET_ADDRESS:
            payload["wallet_address"] = Settings.WALLET_ADDRESS

        logger.debug(f"pop_job payload: {payload}")
        try:
            response = await self.client.post(
                "/v2/generate/text/pop", headers=self.headers, json=payload
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("id"):
                return None
            return data
        except httpx.HTTPStatusError as e:
            logger.error(f"pop_job error [{e.response.status_code}]: {e.response.text}")
            raise

    async def submit_result(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a completed text generation result (retries on transient errors)."""
        job_id = payload.get("id", "?")
        for attempt in range(3):
            try:
                logger.debug(f"Submitting result for job {job_id} (attempt {attempt + 1}/3)")
                response = await self.client.post(
                    "/v2/generate/text/submit", headers=self.headers, json=payload
                )
                if response.status_code == 200:
                    resp_data = response.json()
                    reward = resp_data.get("reward", 0)
                    logger.debug(f"Submit OK — {reward} 電")
                    return resp_data
                else:
                    logger.error(f"Submit error [{response.status_code}]: {response.text[:200]}")
                    response.raise_for_status()
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
                if attempt < 2:
                    logger.warning(f"Submit retry for {job_id}: {e}")
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                raise

    async def find_user(self) -> Optional[Dict[str, Any]]:
        """Look up the current user from the API key."""
        try:
            response = await self.client.get(
                "/v2/find_user", headers=self.headers
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"find_user error: {e}")
            return None

    async def close(self):
        await self.client.aclose()
