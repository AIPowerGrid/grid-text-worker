import asyncio
from inference_worker.ws_client import run_workers

# Multi-backend supervisor: serves EVERY backend in GRID_BACKENDS (each as its
# own grid worker), scaling per-backend concurrency and restarting dead
# connections. This replaced StreamingWorker().run(), which only launched
# load_backends()[0] — so any 2nd/3rd backend (e.g. qwen, deepseek) never
# started.
asyncio.run(run_workers())
