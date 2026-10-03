# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 AI Power Grid
"""Headless-server onboarding: vision probe, /v1 URLs, terminal setup, service install."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from inference_worker import cli, headless, service, vision_probe, ws_client
from inference_worker.detect_backends import backend_base_url, check_backend_url
from inference_worker.ws_client import StreamingWorker


# ── Vision probe ─────────────────────────────────────────────────────────


def _worker(url="http://127.0.0.1:8000/v1", model="flash-next"):
    worker = StreamingWorker.__new__(StreamingWorker)
    worker.spec = SimpleNamespace(backend_type="openai", url=url, model_name=model, api_key="")
    worker.model_name = model
    worker.grid_model_name = f"grid/{model}"
    worker.backend = httpx.AsyncClient(timeout=5)
    return worker


@pytest.fixture(autouse=True)
def _fresh_vision_cache():
    ws_client._VISION_VERDICTS.clear()
    ws_client._VISION_LOCKS.clear()
    yield
    ws_client._VISION_VERDICTS.clear()
    ws_client._VISION_LOCKS.clear()


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
def test_answer_in_either_reasoning_field_counts(field):
    nonce = "7391"
    message = {"content": None, field: "The picture shows the digits 7 3 9 1."}
    assert ws_client._vision_probe_answer_match(message, nonce) == 4


def test_reasoning_needs_the_whole_nonce_as_one_run():
    assert vision_probe.nonce_in_text("I see 7391", "7391")
    assert vision_probe.nonce_in_text("digits: 7, 3, 9, 1", "7391")
    assert not vision_probe.nonce_in_text("173910 tokens", "7391")
    assert not vision_probe.nonce_in_text("about 4 digits, maybe 12", "7391")


@pytest.mark.asyncio
async def test_vision_probe_runs_once_for_all_parallel_slots(monkeypatch):
    nonce = "4826"
    monkeypatch.setattr(vision_probe, "make_nonce", lambda: nonce)
    workers = [_worker() for _ in range(8)]
    with respx.mock:
        route = respx.post("http://127.0.0.1:8000/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "", "reasoning": f"It reads {nonce}"}}]}
            )
        )
        verdicts = await asyncio.gather(*(w._detect_vision() for w in workers))
        assert await workers[0]._detect_vision() is True  # reconnect reuses it
    assert verdicts == [True] * 8
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["max_tokens"] >= 1024
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_strict_server_400_retries_without_template_kwargs(monkeypatch):
    nonce = "1593"
    monkeypatch.setattr(vision_probe, "make_nonce", lambda: nonce)
    bodies = []

    def reply(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "chat_template_kwargs" in body:
            return httpx.Response(400, json={"error": {"message": "unknown field"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": nonce}}]})

    with respx.mock:
        respx.post("http://127.0.0.1:8000/v1/chat/completions").mock(side_effect=reply)
        assert await _worker()._detect_vision() is True
    assert len(bodies) == 2 and "chat_template_kwargs" not in bodies[1]


@pytest.mark.asyncio
async def test_inconclusive_probe_is_not_cached(monkeypatch):
    monkeypatch.setattr(vision_probe, "make_nonce", lambda: "2468")
    with respx.mock:
        route = respx.post("http://127.0.0.1:8000/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"choices": [{"message": {"content": "2468"}}]}),
            ]
        )
        assert await _worker()._detect_vision() is False
        assert await _worker()._detect_vision() is True
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_text_only_model_stays_text_only(monkeypatch):
    monkeypatch.setattr(vision_probe, "make_nonce", lambda: "8642")
    with respx.mock:
        respx.post("http://127.0.0.1:8000/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "NO_IMAGE"}}]})
        )
        assert await _worker()._detect_vision() is False


# ── Backend URLs with a trailing /v1 ─────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000", "http://127.0.0.1:8000/", "http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1/"],
)
def test_backend_base_url_drops_trailing_v1(url):
    assert backend_base_url(url) == "http://127.0.0.1:8000"


@pytest.mark.asyncio
async def test_check_url_with_v1_finds_vllm_models():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://127.0.0.1:8000/api/tags").mock(return_value=httpx.Response(404))
        router.get("http://127.0.0.1:8000/version").mock(
            return_value=httpx.Response(200, json={"version": "0.11.0"})
        )
        router.get("http://127.0.0.1:8000/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "flash-next"}]})
        )
        info = await check_backend_url("http://127.0.0.1:8000/v1")
    assert info["reachable"] and info["engine"] == "vllm"
    assert info["models"] == ["flash-next"]
    assert info["url"] == "http://127.0.0.1:8000"


def test_cli_backend_url_override_does_not_double_v1(monkeypatch):
    from inference_worker.config import Settings

    def no_ollama(*_args, **_kwargs):
        raise httpx.ConnectError("no ollama")

    monkeypatch.setattr(httpx, "get", no_ollama)
    monkeypatch.setattr(Settings, "OPENAI_URL", "")
    args = SimpleNamespace(api_key=None, model=None, worker_name=None,
                           backend_url="http://127.0.0.1:8000/v1/")
    cli._apply_cli_overrides(args)
    assert Settings.OPENAI_URL == "http://127.0.0.1:8000/v1"


# ── One-command terminal setup ───────────────────────────────────────────


@pytest.fixture
def fake_backend(monkeypatch):
    saved = {}

    async def check(url, api_key=""):
        return {"reachable": True, "engine": "vllm", "name": "vLLM", "models": ["flash-next"], "url": url}

    async def context(url, engine=None, model_name=None, api_key=""):
        return {"context_length": 262144}

    monkeypatch.setattr("inference_worker.detect_backends.check_backend_url", check)
    monkeypatch.setattr("inference_worker.detect_backends.get_model_context_length", context)
    monkeypatch.setattr(headless, "_validate_backend", lambda *a: (True, "hello"))
    monkeypatch.setattr(headless, "write_env", saved.update)
    return saved


def test_unattended_setup_reads_model_and_saves_parallel_slots(fake_backend, capsys):
    config = headless.unattended_setup(
        backend_url="http://127.0.0.1:8000/v1", api_key="grid-key", worker_name="rig", concurrency=8,
    )
    backends = json.loads(config["GRID_BACKENDS"])
    assert backends == [{
        "type": "openai", "url": "http://127.0.0.1:8000/v1", "api_key": "", "model": "flash-next",
        "grid_model": "flash-next", "concurrency": 8,
    }]
    assert config["GRID_API_KEY"] == "grid-key"
    assert fake_backend["MODEL_NAME"] == "flash-next"
    assert "262144" in capsys.readouterr().out


def test_unattended_setup_requires_key_for_parallel_slots(fake_backend):
    with pytest.raises(headless.SetupError, match="--api-key"):
        headless.unattended_setup(backend_url="http://127.0.0.1:8000", concurrency=8)


def test_unattended_setup_asks_for_model_when_several_are_served(fake_backend, monkeypatch):
    async def check(url, api_key=""):
        return {"reachable": True, "engine": "vllm", "models": ["a", "b"], "url": url}

    monkeypatch.setattr("inference_worker.detect_backends.check_backend_url", check)
    with pytest.raises(headless.SetupError, match="--model"):
        headless.unattended_setup(backend_url="http://127.0.0.1:8000", api_key="k")


def test_unattended_setup_single_slot_uses_console_approval(fake_backend, monkeypatch):
    approvals = []

    async def approve(name):
        approvals.append(name)
        return {"status": "activated"}

    monkeypatch.setattr(headless, "_authorize_grid_worker", approve)
    config = headless.unattended_setup(backend_url="http://127.0.0.1:8000", worker_name="rig")
    assert approvals == ["rig"]
    assert config["GRID_ENROLLED_WORKER_NAME"] == "rig"
    assert json.loads(config["GRID_BACKENDS"])[0]["name"] == "rig"
    assert "GRID_API_KEY" not in config


def test_cli_setup_then_install_service(monkeypatch):
    calls = {}
    monkeypatch.setattr("sys.argv", [
        "grid-inference-worker", "--setup", "--backend-url", "http://127.0.0.1:8000",
        "--api-key", "k", "--concurrency", "8", "--install-service",
    ])
    monkeypatch.setattr(headless, "unattended_setup", lambda **kw: calls.setdefault("setup", kw) and {"MODEL_NAME": "m"})
    monkeypatch.setattr("inference_worker.env_utils.reload_settings", lambda config: None)
    monkeypatch.setattr("inference_worker.env_utils.is_configured", lambda: True)
    monkeypatch.setattr(service, "install", lambda verbose=True: calls.setdefault("installed", True))
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert calls["setup"]["concurrency"] == 8 and calls["installed"]


def test_cli_rejects_out_of_range_concurrency(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["grid-inference-worker", "--setup", "--concurrency", "99"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "between 1 and 16" in capsys.readouterr().err


# ── Linux service install on a headless server ───────────────────────────


@pytest.mark.parametrize(
    ("tty", "display", "available", "expected"),
    [
        (True, "", {"sudo", "pkexec"}, ["sudo"]),
        (False, ":0", {"sudo", "pkexec"}, ["pkexec"]),
        (False, "", {"sudo", "pkexec"}, None),  # SSH without a TTY: no prompt possible
    ],
)
def test_privilege_launcher_choice(monkeypatch, tty, display, available, expected):
    ran = []
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(service.sys, "stdin", SimpleNamespace(isatty=lambda: tty))
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name in available else None)
    monkeypatch.setattr(
        "subprocess.run", lambda cmd, **kw: ran.append(cmd) or SimpleNamespace(returncode=0)
    )
    ok = service._run_privileged("true")
    if expected is None:
        assert ok is False and ran == []
    else:
        assert ok is True and ran[0][: len(expected)] == expected


def test_failed_privileges_print_manual_unit(monkeypatch, capsys):
    monkeypatch.setattr(service, "_run_privileged", lambda cmds: False)
    monkeypatch.setattr(service.sys, "frozen", False, raising=False)
    assert service._linux_install(verbose=True) is False
    out = capsys.readouterr().out
    assert "[Service]" in out and "ExecStart=" in out
    assert "systemctl enable --now grid-inference-worker" in out
    assert "sudo grid-inference-worker" not in out
