import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    GRID_API_KEY = os.getenv("GRID_API_KEY", "")
    GRID_WORKER_NAME = os.getenv("GRID_WORKER_NAME", "Text-Inference-Worker")
    GRID_API_URL = os.getenv("GRID_API_URL", "https://api.aipowergrid.io/api")
    NSFW = os.getenv("GRID_NSFW", "true").lower() == "true"
    MAX_THREADS = int(os.getenv("GRID_MAX_THREADS", "1"))
    MAX_LENGTH = int(os.getenv("GRID_MAX_LENGTH", "4096"))
    MAX_CONTEXT_LENGTH = int(os.getenv("GRID_MAX_CONTEXT_LENGTH", "4096"))

    # Backend type: "ollama" (easy mode) or "openai" (advanced/custom)
    BACKEND_TYPE = os.getenv("BACKEND_TYPE", "ollama")

    # Ollama settings
    OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")

    # OpenAI-compatible settings (for vllm, sglang, lmdeploy, etc.)
    OPENAI_URL = os.getenv("OPENAI_URL", "http://127.0.0.1:8000/v1")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

    # Model to serve (e.g. "llama3.2:3b" for ollama, "meta-llama/..." for openai)
    MODEL_NAME = os.getenv("MODEL_NAME", "")

    # Grid model name (what to advertise to the grid, with domain prefix)
    GRID_MODEL_NAME = os.getenv("GRID_MODEL_NAME", "")

    # Wallet address for rewards (Base chain)
    WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

    @classmethod
    def validate(cls):
        if not cls.GRID_API_KEY:
            raise RuntimeError("GRID_API_KEY environment variable is required.")
