"""Management-plane authentication, onboarding, and persistence boundaries."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from inference_worker.cli import _verify_runtime_dependencies
from inference_worker.config import Settings
from inference_worker.enrollment import EnrollmentClientError
from inference_worker.gui import _copy_to_clipboard
from inference_worker.web.app import app
from inference_worker.web.app import worker_state
from inference_worker.web.routes import (
    _enrolled_settings_error,
    _model_test_result,
    _safe_next_url,
    _validated_backend_settings,
    _validated_schedule,
    api_grid_canary,
    api_grid_stats,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/", "/"),
        ("/settings?tab=backend", "/settings?tab=backend"),
        ("https://attacker.example", "/"),
        ("//attacker.example/path", "/"),
        ("settings", "/"),
    ],
)
def test_safe_next_url_is_same_origin(value, expected):
    assert _safe_next_url(value) == expected


def test_backend_settings_are_validated_before_persistence():
    form = _validated_backend_settings(
        {
            "OLLAMA_URL": "http://127.0.0.1:11434/",
            "MODEL_NAME": "model",
        }
    )

    assert form["OLLAMA_URL"] == "http://127.0.0.1:11434"
    assert form["MODEL_NAME"] == "model"


def test_backend_settings_reject_metadata_target():
    with pytest.raises(ValueError):
        _validated_backend_settings({"OPENAI_URL": "http://169.254.169.254/v1"})


@pytest.mark.parametrize(
    "settings",
    [
        {"PYTHONPATH": "/tmp/attacker"},
        {"GRID_WORKER_NAME": "worker\nPYTHONPATH=/tmp/attacker"},
        {"BACKEND_TYPE": "shell"},
        {"GRID_NSFW": "maybe"},
        {"GRID_MAX_THREADS": "17"},
        {"GRID_MAX_LENGTH": "not-a-number"},
        {"GRID_MAX_CONTEXT_LENGTH": "131073"},
    ],
)
def test_backend_settings_reject_unsupported_or_malformed_values(settings):
    with pytest.raises(ValueError):
        _validated_backend_settings(settings)


def test_schedule_is_validated_and_canonicalized():
    assert _validated_schedule(
        '[ { "days": "mon-fri", "start": "18:00", "end": "23:59", "concurrency": 0 } ]'
    ) == '[{"days":"mon-fri","start":"18:00","end":"23:59","concurrency":0}]'


@pytest.mark.parametrize(
    "value",
    [
        '{}',
        '[{"days":"funday","concurrency":0}]',
        '[{"start":"25:00","concurrency":0}]',
        '[{"concurrency":17}]',
        '[{"concurrency":true}]',
        '[{"concurrency":0,"command":"shutdown"}]',
    ],
)
def test_schedule_rejects_invalid_windows(value):
    with pytest.raises(ValueError):
        _validated_schedule(value)


def test_enrolled_settings_reject_rename_and_parallel_slots(monkeypatch):
    monkeypatch.setattr(
        Settings, "GRID_ENROLLED_WORKER_NAME", "Text-Inference-Worker-test"
    )
    monkeypatch.setattr(Settings, "MAX_THREADS", 1)

    assert _enrolled_settings_error(
        {"GRID_WORKER_NAME": "different-worker", "GRID_MAX_THREADS": "1"}
    ).startswith("Console-enrolled credentials cannot rename")
    assert _enrolled_settings_error(
        {"GRID_WORKER_NAME": "Text-Inference-Worker-test", "GRID_MAX_THREADS": "2"}
    ).startswith("Console-enrolled credentials support one connection")
    assert _enrolled_settings_error(
        {"GRID_WORKER_NAME": "Text-Inference-Worker-test", "GRID_MAX_THREADS": "1"}
    ) is None
    assert _enrolled_settings_error(
        {
            "GRID_WORKER_NAME": "Text-Inference-Worker-test",
            "GRID_MAX_THREADS": "1",
            "GRID_SCHEDULE": '[{"concurrency":2}]',
        }
    ).startswith("Console-enrolled credentials support one connection")


@pytest.fixture
def dashboard_client(monkeypatch):
    monkeypatch.setattr(Settings, "DASHBOARD_TOKEN", "dashboard-test-token")
    with TestClient(app) as client:
        yield client


def test_login_form_accepts_token_and_sets_strict_cookie(dashboard_client):
    response = dashboard_client.post(
        "/login",
        data={"token": "dashboard-test-token", "next": "/settings?tab=backend"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=backend"
    cookie = response.headers["set-cookie"]
    assert "_token=dashboard-test-token" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie


def test_login_rejects_bad_token_without_crashing(dashboard_client):
    response = dashboard_client.post(
        "/login", data={"token": "wrong", "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 200
    assert "Invalid token" in response.text


def test_login_rejects_external_next_url(dashboard_client):
    response = dashboard_client.post(
        "/login",
        data={"token": "dashboard-test-token", "next": "https://attacker.example"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_query_token_bootstrap_scrubs_url(dashboard_client):
    response = dashboard_client.get(
        "/?view=status&token=dashboard-test-token", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?view=status"
    assert "token=" not in response.headers["location"]


def test_settings_page_never_serializes_stored_credentials(
    dashboard_client, monkeypatch
):
    monkeypatch.setitem(worker_state, "setup_complete", True)
    monkeypatch.setattr(Settings, "GRID_API_KEY", "grid_secret-never-render")
    monkeypatch.setattr(Settings, "OPENAI_API_KEY", "backend-secret-never-render")
    monkeypatch.setattr(
        Settings, "GRID_ENROLLED_WORKER_NAME", "Text-Inference-Worker-test"
    )
    dashboard_client.cookies.set("_token", "dashboard-test-token")

    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert "grid_secret-never-render" not in response.text
    assert "backend-secret-never-render" not in response.text
    assert "Text-Inference-Worker-test" in response.text
    assert "The key is never returned to this page" in response.text


def test_blank_settings_secret_fields_preserve_existing_values(
    dashboard_client, monkeypatch
):
    captured = {}
    monkeypatch.setattr(
        "inference_worker.web.routes.write_env",
        lambda values, **kwargs: captured.setdefault("write", values),
    )
    monkeypatch.setattr(
        "inference_worker.web.routes.reload_settings",
        lambda values: captured.setdefault("reload", values),
    )
    dashboard_client.cookies.set("_token", "dashboard-test-token")

    response = dashboard_client.post(
        "/api/settings",
        json={
            "GRID_API_KEY": "",
            "OPENAI_API_KEY": "",
            "MODEL_NAME": "gpt-oss-20b",
        },
    )

    assert response.status_code == 200
    assert captured["write"] == {"MODEL_NAME": "gpt-oss-20b"}
    assert captured["reload"] == captured["write"]


def test_enrollment_routes_do_not_expose_internal_errors(
    dashboard_client, monkeypatch
):
    async def fail_start(**_kwargs):
        raise EnrollmentClientError(
            "worker enrollment request failed: private-host.internal SECRET_FILE_LOCATION"
        )

    async def fail_poll():
        raise EnrollmentClientError("cannot resume SECRET_PENDING_LOCATION")

    monkeypatch.setattr("inference_worker.web.routes.start_enrollment", fail_start)
    monkeypatch.setattr("inference_worker.web.routes.poll_enrollment", fail_poll)
    dashboard_client.cookies.set("_token", "dashboard-test-token")

    started = dashboard_client.post(
        "/api/setup/enrollment/start",
        json={"worker_name": "Text-Inference-Worker-test", "restart": False},
    )
    polled = dashboard_client.post("/api/setup/enrollment/poll")

    assert started.status_code == 400
    assert polled.status_code == 400
    encoded = started.text + polled.text
    assert "private-host.internal" not in encoded
    assert "SECRET_FILE_LOCATION" not in encoded
    assert "SECRET_PENDING_LOCATION" not in encoded
    assert started.json()["error"].startswith("Could not create worker approval")
    assert polled.json()["error"].startswith("Could not finish worker approval")


@pytest.mark.parametrize(
    ("data", "reply", "reasoning", "finish"),
    [
        ({"choices": [{"message": {"content": "Ready"}, "finish_reason": "stop"}]}, "Ready", False, "stop"),
        ({"choices": [{"message": {"content": "", "reasoning": "private trace"}, "finish_reason": "length"}]}, "", True, "length"),
        ({"choices": []}, "", False, None),
        ({"unexpected": True}, "", False, None),
    ],
)
def test_model_test_result_is_defensive(data, reply, reasoning, finish):
    assert _model_test_result(data) == (reply, reasoning, finish)


def test_runtime_dependencies_can_parse_forms_and_sign_receipts(capsys):
    _verify_runtime_dependencies()
    assert "verified" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_grid_stats_uses_redacted_bound_worker_status(monkeypatch):
    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers=None, params=None):
            if url.endswith("/workers/self"):
                assert headers == {"apikey": "grid_worker_secret"}
                return Response(
                    200,
                    {
                        "schema": "aipg.worker.self.v1",
                        "worker": {
                            "name": "rig-a",
                            "online": True,
                            "maintenance": False,
                            "last_seen": "2026-09-04T12:00:00+00:00",
                            "models": ["model-a"],
                            "job_types": ["text"],
                            "jobs_completed": 7,
                            "den_recorded": 8.25,
                            "account_id": "private-account",
                        },
                        "payout": {
                            "scope": "account",
                            "wallet_configured": True,
                            "latest_status": "confirmed",
                            "last_paid_at": "2026-09-04T11:00:00+00:00",
                            "amount": 999,
                            "address": "private-wallet",
                        },
                    },
                )
            if url.endswith("/workers"):
                return Response(200, {"workers": []})
            return Response(403, {})

    monkeypatch.setattr(Settings, "GRID_API_KEY", "grid_worker_secret")
    monkeypatch.setattr(Settings, "GRID_API_URL", "https://api.aipowergrid.io")
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())

    result = await api_grid_stats()

    assert result["worker"]["name"] == "rig-a"
    assert result["worker"]["jobs_completed"] == 7
    assert result["worker"]["den_earned"] == 8.25
    assert result["payout"] == {
        "scope": "account",
        "wallet_configured": True,
        "latest_status": "confirmed",
        "last_paid_at": "2026-09-04T11:00:00+00:00",
    }
    rendered = str(result)
    assert "grid_worker_secret" not in rendered
    assert "private-account" not in rendered
    assert "private-wallet" not in rendered
    assert "999" not in rendered


@pytest.mark.asyncio
async def test_grid_canary_keeps_worker_key_server_side_and_redacts_core_payload(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {
                "schema": "aipg.worker.canary.v1",
                "status": "passed",
                "worker_name": "rig-a",
                "model": "model-a",
                "latency_ms": 125,
                "reason": "exact_output",
                "proof_scope": "hard_targeted_connectivity_and_exact_output",
                "quality_claim": "none",
                "economic_effect": "none",
                "prompt": "private challenge",
                "output": "private output",
                "account_id": "private account",
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers):
            assert url == "https://api.aipowergrid.io/v1/workers/self/canary"
            assert headers == {"apikey": "grid_worker_secret"}
            return Response()

    monkeypatch.setattr(Settings, "GRID_API_KEY", "grid_worker_secret")
    monkeypatch.setattr(Settings, "GRID_API_URL", "https://api.aipowergrid.io")
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())

    result = await api_grid_canary()

    assert result == {
        "ok": True,
        "status": "passed",
        "worker_name": "rig-a",
        "model": "model-a",
        "latency_ms": 125,
        "reason": "exact_output",
        "proof_scope": "hard_targeted_connectivity_and_exact_output",
        "quality_claim": "none",
        "economic_effect": "none",
    }
    rendered = str(result)
    assert "grid_worker_secret" not in rendered
    assert "private challenge" not in rendered
    assert "private output" not in rendered
    assert "private account" not in rendered


@pytest.mark.asyncio
async def test_grid_canary_rejects_missing_key_and_invalid_core_result(monkeypatch):
    monkeypatch.setattr(Settings, "GRID_API_KEY", "")
    missing = await api_grid_canary()
    assert missing.status_code == 409

    class Response:
        status_code = 200

        def json(self):
            return {
                "schema": "aipg.worker.canary.v1",
                "status": "passed",
                "economic_effect": "paid",
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(Settings, "GRID_API_KEY", "grid_worker_secret")
    monkeypatch.setattr(Settings, "GRID_API_URL", "https://api.aipowergrid.io")
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())
    invalid = await api_grid_canary()
    assert invalid.status_code == 502
    assert b"invalid canary result" in invalid.body


def test_dashboard_link_copy_is_explicit_and_local():
    calls = []
    root = SimpleNamespace(
        clipboard_clear=lambda: calls.append(("clear", None)),
        clipboard_append=lambda value: calls.append(("append", value)),
        update=lambda: calls.append(("update", None)),
    )
    _copy_to_clipboard(root, "http://localhost:7861?token=secret")
    assert calls == [
        ("clear", None),
        ("append", "http://localhost:7861?token=secret"),
        ("update", None),
    ]
