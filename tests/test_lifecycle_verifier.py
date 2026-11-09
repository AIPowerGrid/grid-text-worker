# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Safety boundaries for the separate native lifecycle rehearsal."""

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


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


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_stop_child_targets_only_the_owned_process(monkeypatch, platform):
    calls = []
    process = SimpleNamespace(
        pid=1234,
        poll=lambda: None,
        terminate=lambda: calls.append("terminate"),
        kill=lambda: calls.append("kill"),
        wait=lambda **kw: calls.append(("wait", kw["timeout"])),
    )
    monkeypatch.setattr(verifier.sys, "platform", platform)
    monkeypatch.setattr(
        verifier.subprocess, "run", lambda command, **kw: calls.append((command, kw))
    )

    verifier.stop_child(process)

    if platform == "win32":
        command, options = calls[0]
        assert command == ["taskkill", "/PID", "1234", "/T", "/F"]
        assert options == {
            "check": True,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 10,
        }
    else:
        assert calls[0] == "terminate"
    assert calls[-1] == ("wait", 10)


def test_stop_child_does_not_signal_an_exited_pid(monkeypatch):
    process = SimpleNamespace(poll=lambda: 0, wait=lambda **kw: 0)
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("signalled an exited PID"),
    )
    verifier.stop_child(process)
