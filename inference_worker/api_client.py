"""Retired: the legacy /v2 HTTP poll client.

The grid's /v2 queue is gone — the worker is streaming-only now (a persistent
WebSocket to /v1; see ws_client.run_workers). This stub stays so any stale import
or legacy code path fails loud instead of silently polling a dead endpoint.
"""

import logging

logger = logging.getLogger(__name__)

_RETIRED = (
    "AI Power Grid /v2 polling is retired. This worker is streaming-only "
    "(WebSocket /v1). Set GRID_STREAMING=true (the default) and run via "
    "run_worker.py / `grid-inference-worker`."
)


class APIClient:
    """Legacy poll client — no longer functional."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(_RETIRED)
