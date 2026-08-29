"""Management-plane authentication, onboarding, and persistence boundaries."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from inference_worker.cli import _verify_runtime_dependencies
from inference_worker.config import Settings
from inference_worker.gui import _copy_to_clipboard
from inference_worker.web.app import app
from inference_worker.web.routes import (
    _model_test_result,
    _safe_next_url,
    _validated_backend_settings,
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
