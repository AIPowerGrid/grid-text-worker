# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Saved roster contracts, with isolated config and no external services."""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from inference_worker import env_utils
from inference_worker.config import Settings, load_backends
from inference_worker.web import routes
from inference_worker.web.app import app
from inference_worker.worker_identity import WorkerIdentityError
from inference_worker.ws_client import StreamingWorker, effective_concurrency


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_BACKENDS", "")
    for key, value in {
        "GRID_API_KEY": "",
        "MODEL_NAME": "",
        "GRID_WORKER_NAME": "test-rig",
        "GRID_ENROLLED_WORKER_NAME": "",
        "GRID_SCHEDULE": "",
        "MAX_THREADS": 1,
        "OPENAI_API_KEY": "",
        "DASHBOARD_TOKEN": "local-review-fixture",
    }.items():
        monkeypatch.setattr(Settings, key, value)
    # Restore every field reload_settings can update between tests.
    for key in (
        "BACKEND_TYPE",
        "OLLAMA_URL",
        "OPENAI_URL",
        "GRID_MODEL_NAME",
        "MAX_CONTEXT_LENGTH",
        "MAX_LENGTH",
        "NSFW",
    ):
        monkeypatch.setattr(Settings, key, getattr(Settings, key))
    monkeypatch.setattr(env_utils, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(routes, "start_worker", AsyncMock())
    monkeypatch.setattr(routes, "stop_worker", AsyncMock())
    with TestClient(
        app, headers={"Authorization": "Bearer local-review-fixture"}
    ) as client:
        yield client


def entry(**values):
    return {
        "name": "test-rig",
        "type": "openai",
        "url": "http://127.0.0.1:8000/v1",
        "model": "qwen",
        "grid_model": "qwen",
        "concurrency": 1,
        **values,
    }


def save(client, entries, **values):
    return client.post(
        "/api/settings", json={"GRID_BACKENDS": json.dumps(entries), **values}
    )


def test_enrolled_roster_survives_save_and_registration(manager, monkeypatch):
    monkeypatch.setattr(Settings, "GRID_ENROLLED_WORKER_NAME", "test-rig")
    result = manager.post(
        "/api/setup/complete",
        json={
            "MODEL_NAME": "qwen",
            "GRID_WORKER_NAME": "test-rig",
            "GRID_BACKENDS": json.dumps([entry()]),
            "GRID_SCHEDULE": "",
        },
    )
    assert result.status_code == 200
    backend = load_backends()[0]
    proof = {"fixture": True}
    monkeypatch.setattr(
        "inference_worker.ws_client.build_registration_proof", lambda **kw: proof
    )
    worker = SimpleNamespace(name=backend.name, grid_model_name=backend.grid_model_name)
    assert StreamingWorker._registration_identity(worker) is proof
    worker.name += "-different"
    with pytest.raises(WorkerIdentityError):
        StreamingWorker._registration_identity(worker)


@pytest.mark.parametrize(
    "entries",
    [
        [entry(name="other")],
        [entry(), entry(name="second", model="other")],
        [entry(concurrency=2)],
        [entry(schedule='[{"concurrency":2}]')],
    ],
)
def test_enrolled_roster_rejects_broader_configuration(manager, monkeypatch, entries):
    monkeypatch.setattr(Settings, "GRID_ENROLLED_WORKER_NAME", "test-rig")
    assert save(manager, entries).status_code == 400
    assert not env_utils.ENV_PATH.exists()


def test_enrolled_add_does_not_restart_or_modify_rig(manager, monkeypatch):
    monkeypatch.setattr(Settings, "GRID_ENROLLED_WORKER_NAME", "test-rig")
    assert (
        manager.post("/api/backends/add", json=entry(model="other")).status_code == 409
    )
    routes.start_worker.assert_not_called()
    assert not env_utils.ENV_PATH.exists()


def test_inherited_schedule_survives_mutations_and_cold_reload(manager, monkeypatch):
    raw = '[{"days":"mon","concurrency":0},{"days":"tue","concurrency":0}]'
    assert save(manager, [entry()], GRID_SCHEDULE=raw).status_code == 200
    assert effective_concurrency(load_backends()[0], datetime(2026, 9, 7, 12)) == 0
    assert effective_concurrency(load_backends()[0], datetime(2026, 9, 9, 12)) == 1
    for paused in (True, False):
        assert (
            manager.post(
                "/api/backends/pause", json={"name": "test-rig", "paused": paused}
            ).status_code
            == 200
        )
    persisted = env_utils.read_env()
    assert "schedule" not in json.loads(persisted["GRID_BACKENDS"])[0]
    monkeypatch.setenv("GRID_BACKENDS", persisted["GRID_BACKENDS"])
    monkeypatch.setattr(Settings, "GRID_SCHEDULE", persisted["GRID_SCHEDULE"])
    assert effective_concurrency(load_backends()[0], datetime(2026, 9, 8, 12)) == 0


def test_explicit_override_and_modality_survive_pause_other_backend(manager):
    assert (
        save(
            manager,
            [
                entry(modalities=["text"], schedule=""),
                entry(name="second", model="other", schedule='[{"concurrency":0}]'),
            ],
            GRID_SCHEDULE='[{"concurrency":0}]',
        ).status_code
        == 200
    )
    assert (
        manager.post(
            "/api/backends/pause", json={"name": "second", "paused": True}
        ).status_code
        == 200
    )
    first = load_backends()[0]
    assert first.modalities_declared and first.modalities == ["text"]
    assert first.schedule_declared and first.schedule == ""
    assert effective_concurrency(first) == 1


def test_all_week_pause_and_clear_apply_to_roster(manager):
    assert (
        save(manager, [entry()], GRID_SCHEDULE='[{"concurrency":0}]').status_code == 200
    )
    for day in range(7, 14):
        assert (
            effective_concurrency(load_backends()[0], datetime(2026, 9, day, 23, 59))
            == 0
        )
    assert save(manager, [entry()], GRID_SCHEDULE="").status_code == 200
    assert effective_concurrency(load_backends()[0]) == 1
    assert "GRID_SCHEDULE" not in env_utils.read_env()


def test_per_backend_edits_apply_and_do_not_copy_key_to_new_endpoint(manager):
    assert save(manager, [entry(api_key="old-fixture")]).status_code == 200
    assert (
        save(manager, [entry(concurrency=3, api_key="replacement-fixture")]).status_code
        == 200
    )
    backend = load_backends()[0]
    assert backend.concurrency == 3 and backend.api_key == "replacement-fixture"
    assert (
        save(
            manager, [entry(url="http://127.0.0.1:9000/v1", concurrency=2)]
        ).status_code
        == 200
    )
    backend = load_backends()[0]
    assert backend.url == "http://127.0.0.1:9000/v1" and backend.concurrency == 2
    assert backend.api_key == ""


@pytest.mark.parametrize(
    "patch", [{"concurrency": "bad"}, {"type": "shell"}, {"paused": "false"}]
)
def test_roster_validation_rejects_bad_types_without_persistence(manager, patch):
    assert save(manager, [entry(**patch)]).status_code == 400
    assert not env_utils.ENV_PATH.exists()


def test_duplicate_names_are_rejected(manager):
    assert save(manager, [entry(), entry(model="other")]).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cap", "detected", "expected"),
    [(32768, 8192, 8192), (4096, 8192, 4096), (0, 8192, 8192)],
)
async def test_context_cap_never_exceeds_detected_backend(cap, detected, expected):
    worker = object.__new__(StreamingWorker)
    worker.spec = SimpleNamespace(max_context=cap)
    worker._detect_context_auto = AsyncMock(return_value=detected)
    assert await worker._detect_context() == expected
    worker._detect_context_auto.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_detection_failure_is_conservative(monkeypatch):
    worker = object.__new__(StreamingWorker)
    worker.spec = SimpleNamespace(
        max_context=32768,
        url="http://127.0.0.1:8000/v1",
        backend_type="openai",
        model_name="qwen",
        api_key="",
    )
    monkeypatch.setattr(Settings, "MAX_CONTEXT_LENGTH", 131072)
    monkeypatch.setattr(
        "inference_worker.detect_backends.get_model_context_length",
        AsyncMock(return_value={"context_length": None}),
    )
    assert await worker._detect_context() == 8192
