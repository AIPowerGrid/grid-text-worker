"""Management-plane authentication and persistence boundaries."""

import pytest

from inference_worker.web.routes import _safe_next_url, _validated_backend_settings


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
