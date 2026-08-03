"""Smoke tests for grid-inference-worker.

Verifies the pure-function utility surface compiles and runs without a
real backend connection. Doesn't cover the running worker (that needs
Ollama + a Grid API to talk to) — that belongs in integration tests
once we have an in-CI Ollama fixture.

These tests ARE in scope for CI today.
"""

import platform
from unittest.mock import MagicMock

import pytest

from inference_worker.detect_backends import (
    KNOWN_ENGINES,
    _extract_models_openai,
    _identify_engine_from_headers,
    get_platform,
)
from inference_worker.ws_client import _normalize_stream_delta


# ============ KNOWN_ENGINES table shape ============


def test_known_engines_contains_ollama_and_vllm():
    names = {e["name"].lower() for e in KNOWN_ENGINES}
    assert "ollama" in names
    assert "vllm" in names


def test_every_known_engine_has_required_keys():
    """Each engine entry must have name, default_port, probes — the detector
    iterates blindly over this table."""
    for engine in KNOWN_ENGINES:
        assert "name" in engine
        assert "default_port" in engine
        assert isinstance(engine["default_port"], int)
        assert engine["default_port"] > 0
        assert "probes" in engine
        assert isinstance(engine["probes"], list)
        for probe in engine["probes"]:
            assert "path" in probe
            assert probe["path"].startswith("/")
            assert "engine" in probe


def test_no_duplicate_default_ports_in_known_engines():
    """Two engines colliding on a port would make detection ambiguous."""
    ports = [e["default_port"] for e in KNOWN_ENGINES]
    assert len(ports) == len(set(ports)), f"duplicate ports: {ports}"


# ============ _extract_models_openai ============


def test_extract_models_openai_returns_ids():
    data = {"data": [{"id": "llama-3-8b"}, {"id": "mistral-7b"}]}
    assert _extract_models_openai(data) == ["llama-3-8b", "mistral-7b"]


def test_extract_models_openai_handles_missing_data_key():
    assert _extract_models_openai({}) == []


def test_extract_models_openai_skips_entries_without_id():
    data = {"data": [{"id": "llama-3-8b"}, {"name": "no-id"}, {"id": ""}]}
    assert _extract_models_openai(data) == ["llama-3-8b"]


# ============ _identify_engine_from_headers ============


def test_identify_engine_from_headers_detects_vllm():
    assert _identify_engine_from_headers({"server": "vllm/0.4.0"}) == "vllm"
    assert _identify_engine_from_headers({"server": "VLLM"}) == "vllm"  # case-insensitive


def test_identify_engine_from_headers_returns_none_for_uvicorn():
    # Many engines run uvicorn — not definitive
    assert _identify_engine_from_headers({"server": "uvicorn"}) is None


def test_identify_engine_from_headers_returns_none_for_unknown():
    assert _identify_engine_from_headers({"server": "nginx"}) is None
    assert _identify_engine_from_headers({}) is None


# ============ get_platform ============


def test_get_platform_returns_expected_value():
    result = get_platform()
    assert result in ("linux", "macos", "windows")

    # Sanity-check against actual host
    system = platform.system().lower()
    if system == "darwin":
        assert result == "macos"
    elif system == "windows":
        assert result == "windows"
    else:
        assert result == "linux"


def test_vllm_reasoning_delta_is_normalized_without_mutating_source():
    source = {"role": "assistant", "reasoning": "working"}

    normalized = _normalize_stream_delta(source)

    assert normalized == {"role": "assistant", "reasoning_content": "working"}
    assert source == {"role": "assistant", "reasoning": "working"}


def test_canonical_reasoning_delta_is_preserved():
    source = {"reasoning_content": "working"}

    assert _normalize_stream_delta(source) is source
