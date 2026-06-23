import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..env_utils import is_configured, ensure_dashboard_token

logger = logging.getLogger(__name__)

# Ring buffer for log lines (last 500)
log_buffer = deque(maxlen=500)


class BufferHandler(logging.Handler):
    # Skip noisy log lines from the buffer
    _SKIP = ("HTTP Request:", "GET /api/", "POST /api/", "GET /static/")

    def emit(self, record):
        msg = self.format(record)
        if any(s in msg for s in self._SKIP):
            return
        log_buffer.append(msg)


def setup_log_capture():
    """Attach a buffer handler to the root logger to capture worker output."""
    handler = BufferHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(handler)


import sys

if getattr(sys, "frozen", False):
    # Running as PyInstaller bundle — data files are in sys._MEIPASS
    WEB_DIR = Path(sys._MEIPASS) / "inference_worker" / "web"
else:
    WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# Shared state so routes can inspect the worker
worker_state = {
    "running": False,
    "task": None,
    "worker": None,
    "error": None,
    "setup_complete": False,
}


async def _run_worker():
    """Run the multi-backend streaming supervisor as a background task.

    Serves every backend in GRID_BACKENDS over WebSocket (/v1). The legacy /v2
    HTTP poll loop (TextWorker) is retired along with the grid's /v2 API, so the
    worker is streaming-only now."""
    from ..ws_client import run_workers
    worker_state["worker"] = None
    worker_state["running"] = True
    worker_state["error"] = None
    logger.info("⚡ Streaming mode — connecting backend(s) via WebSocket")
    try:
        await run_workers()
    except asyncio.CancelledError:
        logger.info("Worker task cancelled.")
    except Exception as e:
        logger.error(f"Worker error: {e}")
        worker_state["error"] = str(e)
    finally:
        worker_state["running"] = False


async def start_worker():
    """Start the worker (called after setup or on startup if configured)."""
    if worker_state.get("task") and not worker_state["task"].done():
        return  # already running
    task = asyncio.create_task(_run_worker())
    worker_state["task"] = task


async def stop_worker():
    """Stop the worker."""
    task = worker_state.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_log_capture()
    ensure_dashboard_token()
    if is_configured():
        logger.info("Config found — starting worker.")
        worker_state["setup_complete"] = True
        await start_worker()
    else:
        logger.info("No config — serving setup wizard.")

    yield

    await stop_worker()
    logger.info("Shutdown complete.")


app = FastAPI(title="Grid Inference Worker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Import routes after app is created
from . import routes  # noqa: E402, F401
