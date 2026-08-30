"""Shared .env helpers — single source of truth for reading/writing config."""

import os
import sys

from .config import ENV_FILE, Settings

ENV_PATH = ENV_FILE


def read_env() -> dict:
    """Read .env into a dict, skipping comments and blanks."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def write_env(config: dict, *, delete_empty: bool = False):
    """Write config dict to .env, preserving existing keys.

    If delete_empty is True, keys with empty/None values are removed from .env
    (used by the settings page when a user clears a field).
    """
    env = read_env()
    for k, v in config.items():
        if v is not None and v != "":
            env[k] = str(v)
        elif delete_empty and k in env:
            del env[k]
    # Strip newlines from values to prevent .env injection
    content = "\n".join(f"{k}={v.replace(chr(10), '').replace(chr(13), '')}" for k, v in env.items()) + "\n"
    ENV_PATH.write_text(content)
    # Restrict permissions on Unix — file contains API keys
    if sys.platform != "win32":
        os.chmod(ENV_PATH, 0o600)


def reload_settings(config: dict):
    """Push a config dict into the in-memory Settings class."""
    _STR = {
        "GRID_API_KEY": "GRID_API_KEY",
        "GRID_ENROLLED_WORKER_NAME": "GRID_ENROLLED_WORKER_NAME",
        "GRID_WORKER_NAME": "GRID_WORKER_NAME",
        "BACKEND_TYPE": "BACKEND_TYPE",
        "OLLAMA_URL": "OLLAMA_URL",
        "OPENAI_URL": "OPENAI_URL",
        "OPENAI_API_KEY": "OPENAI_API_KEY",
        "MODEL_NAME": "MODEL_NAME",
        "GRID_MODEL_NAME": "GRID_MODEL_NAME",
        "WALLET_ADDRESS": "WALLET_ADDRESS",
    }
    for env_key, attr in _STR.items():
        if env_key in config and config[env_key]:
            setattr(Settings, attr, config[env_key])

    if "GRID_NSFW" in config:
        Settings.NSFW = str(config["GRID_NSFW"]).lower() == "true"
    if "GRID_MAX_THREADS" in config:
        Settings.MAX_THREADS = int(config["GRID_MAX_THREADS"])
    if "GRID_MAX_LENGTH" in config:
        Settings.MAX_LENGTH = int(config["GRID_MAX_LENGTH"])
    if "GRID_MAX_CONTEXT_LENGTH" in config:
        Settings.MAX_CONTEXT_LENGTH = int(config["GRID_MAX_CONTEXT_LENGTH"])


def is_configured() -> bool:
    """Check if minimum config exists to run the worker."""
    return bool(Settings.GRID_API_KEY and Settings.MODEL_NAME)


def ensure_dashboard_token() -> str:
    """Create a random dashboard auth token if one doesn't exist yet."""
    if Settings.DASHBOARD_TOKEN:
        return Settings.DASHBOARD_TOKEN
    import secrets
    token = secrets.token_urlsafe(32)
    write_env({"DASHBOARD_TOKEN": token})
    Settings.DASHBOARD_TOKEN = token
    return token
