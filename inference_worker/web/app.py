import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..worker import TextWorker

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


def _is_configured() -> bool:
    """Check if minimum config exists to run the worker."""
    return bool(Settings.GRID_API_KEY and Settings.MODEL_NAME)


async def _run_worker():
    """Run the text worker loop as a background task."""
    worker = TextWorker()
    worker_state["worker"] = worker
    worker_state["running"] = True
    worker_state["error"] = None
    try:
        await worker.run()
    except asyncio.CancelledError:
        logger.info("Worker task cancelled.")
    except Exception as e:
        logger.error(f"Worker error: {e}")
        worker_state["error"] = str(e)
    finally:
        worker_state["running"] = False
        await worker.cleanup()


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
    if _is_configured():
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
