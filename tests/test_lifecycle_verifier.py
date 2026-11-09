# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Safety boundaries for the separate native lifecycle rehearsal."""

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify-worker-lifecycle.py"
SPEC = importlib.util.spec_from_file_location("verify_worker_lifecycle", SCRIPT)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def test_child_environment_excludes_host_configuration(monkeypatch, tmp_path):
    for key in (
        "GRID_API_KEY",
        "OPENAI_API_KEY",
        "DASHBOARD_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GRID_BACKENDS",
        "HTTP_PROXY",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "host-fixture-must-not-propagate")

    env = verifier.child_environment(tmp_path, 12345, 23456)

    assert "host-fixture-must-not-propagate" not in env.values()
    assert env["GRID_API_KEY"] == verifier.KEY
    assert env["HOME"] == env["USERPROFILE"] == str(tmp_path)
    assert env["OPENAI_URL"] == "http://127.0.0.1:12345/v1"
    assert env["GRID_API_URL"] == env["GRID_STREAMING_URL"] == "http://127.0.0.1:23456"
    assert env["GRID_BACKEND_READ_TIMEOUT"] == "10"
    assert (tmp_path / "config").is_dir() and (tmp_path / "tmp").is_dir()
