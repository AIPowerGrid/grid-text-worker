# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 AI Power Grid
"""Loaded-instance context, not catalog capacity, governs worker admission."""

from types import SimpleNamespace

import httpx
import pytest
import respx

from inference_worker.config import Settings
from inference_worker.detect_backends import get_model_context_length
from inference_worker.ws_client import StreamingWorker


def model(key, instances):
    return {
        "key": key,
        "max_context_length": 131072,
        "loaded_instances": [
            {"id": name, "config": {"context_length": context}}
            for name, context in instances
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("models", "requested", "expected"),
    [
        (
            [model("openai/gpt-oss-120b", [("gpt-oss-120b", 131072)])],
            "gpt-oss-120b",
            131072,
        ),
        (
            [model("catalog/model", [("first", 4096), ("selected", 32768)])],
            "selected",
            32768,
        ),
        (
            [model("catalog/model", [("first", 32768), ("second", 4096)])],
            "catalog/model",
            4096,
        ),
        (
            [
                model("selected", [("other", 65536)]),
                model("catalog/actual", [("selected", 16384)]),
            ],
            "selected",
            16384,
        ),
        ([model("catalog/model", [])], "catalog/model", None),
        ([model("catalog/model", [("first", 4096)])], "missing", None),
        ([model("catalog/model", [("first", 4096), ("second", 32768)])], None, 4096),
        (
            [model("catalog/model", [("first", 32768), ("second", None)])],
            "catalog/model",
            None,
        ),
        ([model("catalog/model", [("first", None)])], "first", None),
        ([model("catalog/model", [("first", 0)])], "first", None),
        ([model("catalog/model", [("first", -1)])], "first", None),
        ([model("catalog/model", [("first", True)])], "first", None),
        ([model("catalog/model", [("first", "131072")])], "first", None),
        ([model("catalog/model", [("first", 8192.5)])], "first", None),
        ([None, model("catalog/model", [("first", 8192)])], "first", 8192),
        ([{"key": "catalog/model", "loaded_instances": [None]}], "catalog/model", None),
        (
            [
                {
                    "key": "catalog/model",
                    "loaded_instances": [{"id": "first", "config": None}],
                }
            ],
            "first",
            None,
        ),
        (None, "first", None),
    ],
)
async def test_loaded_instance_context(models, requested, expected):
    with respx.mock:
        route = respx.get("http://127.0.0.1:1234/api/v1/models").mock(
            return_value=httpx.Response(200, json={"models": models})
        )
        result = await get_model_context_length(
            "http://127.0.0.1:1234", "lmstudio", requested
        )
    assert result == {"context_length": expected}
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "expected"), [(131072, 131072), (32768, 32768)])
async def test_worker_registration_context_uses_alias_and_preserves_cap(
    monkeypatch, cap, expected
):
    monkeypatch.setattr(Settings, "MAX_CONTEXT_LENGTH", 131072)
    worker = StreamingWorker.__new__(StreamingWorker)
    worker.spec = SimpleNamespace(
        backend_type="lmstudio",
        url="http://127.0.0.1:1234/v1",
        model_name="gpt-oss-120b",
        api_key="",
        max_context=cap,
    )
    with respx.mock:
        respx.get("http://127.0.0.1:1234/api/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [model("openai/gpt-oss-120b", [("gpt-oss-120b", 131072)])]
                },
            )
        )
        assert await worker._detect_context() == expected
