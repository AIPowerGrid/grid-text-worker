# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Qualify one native executable against loopback-only synthetic peers.

No real Core, inference model, enrollment, API credential, or economic action.
Child configuration/signing state is private, disposable and never uploaded.
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve


MODEL = "lifecycle-fixture-model"
KEY = "local-test-only"
PROTOCOLS = [
    ("openai-chat", True),
    ("openai-responses", True),
    ("anthropic", True),
    ("openai-responses", False),
    ("anthropic", False),
]


class Backend:
    def __init__(self) -> None:
        self.started = {i: threading.Event() for i in range(len(PROTOCOLS))}
        self.closed = {i: threading.Event() for i in range(len(PROTOCOLS))}


def handler_type(state: Backend) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            return

        def respond(self, body: dict | str, streaming: bool = False) -> None:
            encoded = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream" if streaming else "application/json"
            )
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()
            self.close_connection = True

        def do_GET(self) -> None:
            if self.path != "/v1/models":
                self.send_error(404)
                return
            self.respond({"data": [{"id": MODEL, "max_model_len": 4096}]})

        def do_POST(self) -> None:
            if self.path not in {
                "/v1/chat/completions",
                "/v1/responses",
                "/v1/messages",
            }:
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 200000:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length))
            messages = body.get("messages") or []
            marker = body.get("input") or (
                messages[-1].get("content", "") if messages else ""
            )
            if marker.startswith("lifecycle-timeout-") or marker.startswith(
                "lifecycle-cancel-"
            ):
                index = int(marker.rsplit("-", 1)[1])
                is_cancel = marker.startswith("lifecycle-cancel-")
                if is_cancel:
                    state.started[index].set()
                self.connection.settimeout(25)
                try:
                    # The worker must close this pending HTTP request on its
                    # 10s read timeout or on the received Grid cancel frame.
                    if self.connection.recv(1) == b"" and is_cancel:
                        state.closed[index].set()
                except ConnectionResetError:
                    if is_cancel:
                        state.closed[index].set()
                except TimeoutError:
                    return
                finally:
                    self.close_connection = True
                return
            usage = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
            if body.get("stream"):
                chunk = {
                    "choices": [
                        {"delta": {"content": "fixture-ok"}, "finish_reason": "stop"}
                    ],
                    "usage": usage,
                }
                self.respond(
                    "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n", True
                )
            elif self.path.endswith("/responses"):
                self.respond(
                    {
                        "output": [
                            {"content": [{"type": "output_text", "text": "fixture-ok"}]}
                        ],
                        "usage": usage,
                    }
                )
            elif self.path.endswith("/messages"):
                self.respond(
                    {
                        "content": [{"type": "text", "text": "fixture-ok"}],
                        "usage": usage,
                    }
                )
            else:
                self.respond(
                    {
                        "choices": [{"message": {"content": "fixture-ok"}}],
                        "usage": usage,
                    }
                )

    return Handler


def assignment(index: int, mode: str) -> str:
    api_format, stream = PROTOCOLS[index]
    marker = f"lifecycle-{mode}-{index}"
    request: dict[str, Any] = {"stream": stream, "max_tokens": 32}
    if api_format == "openai-responses":
        request["input"] = marker
    else:
        request["messages"] = [{"role": "user", "content": marker}]
    return json.dumps(
        {
            "type": "job",
            "id": marker,
            "payload": {"api_format": api_format, "request": request},
        }
    )


async def terminal(socket: Any, job_id: str) -> tuple[dict, list[dict]]:
    output = []
    async with asyncio.timeout(35):
        while True:
            message = json.loads(await socket.recv())
            if message.get("type") == "pong":
                continue
            if message.get("id") != job_id:
                raise ValueError("unexpected job binding")
            if message.get("type") in {"done", "error"}:
                return message, output
            output.append(message)


def child_environment(home: Path, http_port: int, ws_port: int) -> dict[str, str]:
    # Do not inherit host credentials, Python paths, proxies or worker settings.
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "SYSTEMDRIVE",
        }
    }
    config = home / "config"
    temporary = home / "tmp"
    config.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(config),
            "LOCALAPPDATA": str(config),
            "APPDATA": str(config),
            "TMP": str(temporary),
            "TEMP": str(temporary),
            "TMPDIR": str(temporary),
            "GRID_API_URL": f"http://127.0.0.1:{ws_port}",
            "GRID_STREAMING_URL": f"http://127.0.0.1:{ws_port}",
            "GRID_API_KEY": KEY,
            "GRID_WORKER_NAME": "local-lifecycle-fixture",
            "MODEL_NAME": MODEL,
            "GRID_MODEL_NAME": MODEL,
            "BACKEND_TYPE": "openai",
            "OPENAI_URL": f"http://127.0.0.1:{http_port}/v1",
            "GRID_MODALITIES": "text",
            "GRID_MAX_THREADS": "1",
            "GRID_MAX_LENGTH": "64",
            "GRID_BACKEND_READ_TIMEOUT": "10",
            "DASHBOARD_TOKEN": "local-fixture-session",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


async def verify(command: list[str], frozen: bool) -> dict:
    state = Backend()
    backend = ThreadingHTTPServer(("127.0.0.1", 0), handler_type(state))
    backend.daemon_threads = True
    thread = threading.Thread(
        target=backend.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    results = []
    complete = asyncio.get_running_loop().create_future()

    async def coordinator(socket: Any) -> None:
        try:
            async with asyncio.timeout(45):
                registration = json.loads(await socket.recv())
            if registration.get("apikey") != KEY or registration.get("models") != [
                MODEL
            ]:
                raise ValueError("unexpected fixture registration")
            await socket.send(
                json.dumps({"type": "ready", "worker_id": "local-fixture"})
            )
            for index, (api_format, stream) in enumerate(PROTOCOLS):
                await socket.send(assignment(index, "timeout"))
                failed, _ = await terminal(socket, f"lifecycle-timeout-{index}")
                if failed.get("type") != "error" or "ReadTimeout" not in failed.get(
                    "message", ""
                ):
                    raise ValueError("timeout was not classified")
                await socket.send(assignment(index, "success"))
                succeeded, output = await terminal(socket, f"lifecycle-success-{index}")
                if succeeded.get("type") != "done" or "fixture-ok" not in json.dumps(
                    [succeeded, output]
                ):
                    raise ValueError("next assignment did not complete")
                await socket.send(
                    json.dumps({"type": "ack", "id": succeeded["id"], "den": 0})
                )
                state.closed[index].clear()
                await socket.send(assignment(index, "cancel"))
                if not await asyncio.to_thread(state.started[index].wait, 5):
                    raise ValueError("cancel request did not start")
                await socket.send(
                    json.dumps({"type": "cancel", "id": f"lifecycle-cancel-{index}"})
                )
                cancelled, _ = await terminal(socket, f"lifecycle-cancel-{index}")
                if not cancelled.get("cancelled") or not await asyncio.to_thread(
                    state.closed[index].wait, 5
                ):
                    raise ValueError("pending HTTP request was not aborted")
                await socket.send(
                    json.dumps({"type": "ack", "id": cancelled["id"], "den": 0})
                )
                results.append(
                    {
                        "format": api_format,
                        "stream": stream,
                        "timeout": True,
                        "next_job": True,
                        "cancel_closed_http": True,
                    }
                )
            if not complete.done():
                complete.set_result(True)
        except Exception as exc:
            if not complete.done():
                complete.set_exception(RuntimeError(type(exc).__name__))

    try:
        async with serve(coordinator, "127.0.0.1", 0) as server:
            ws_port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory(prefix="worker-lifecycle-") as private:
                home = Path(private)
                with socket.socket() as reserved:
                    reserved.bind(("127.0.0.1", 0))
                    dashboard_port = reserved.getsockname()[1]
                fd = os.open(
                    home / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(fd, "wb") as log:
                    process = subprocess.Popen(
                        [
                            *command,
                            "--no-gui",
                            "--no-browser",
                            "--port",
                            str(dashboard_port),
                        ],
                        cwd=home,
                        env=child_environment(home, backend.server_port, ws_port),
                        stdout=log,
                        stderr=log,
                    )
                    try:
                        async with asyncio.timeout(180):
                            while not complete.done():
                                if process.poll() is not None:
                                    raise RuntimeError(
                                        "worker exited before qualification"
                                    )
                                await asyncio.sleep(0.1)
                            await complete
                        try:
                            opener = urllib.request.build_opener(
                                urllib.request.ProxyHandler({})
                            )
                            await asyncio.to_thread(
                                opener.open,
                                f"http://127.0.0.1:{dashboard_port}/api/status",
                                timeout=5,
                            )
                        except urllib.error.HTTPError as exc:
                            if exc.code != 401:
                                raise
                        else:
                            raise ValueError(
                                "dashboard accepted unauthenticated request"
                            )
                    finally:
                        if process.poll() is None:
                            process.terminate()
                        try:
                            await asyncio.to_thread(process.wait, 10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            await asyncio.to_thread(process.wait, 5)
    finally:
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)
    return {
        "schema": "aipg.worker.local-lifecycle.v1",
        "frozen": frozen,
        "passed": True,
        "dashboard_unauthenticated_status": 401,
        "protocols": results,
        "limits": "Synthetic loopback peers only; not live Core, real inference, enrollment or economic qualification.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--binary", type=Path)
    mode.add_argument(
        "--source",
        action="store_true",
        help="Developer rehearsal only, never frozen qualification",
    )
    args = parser.parse_args()
    if args.source:
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "run_frozen.py"),
        ]
    else:
        binary = args.binary.resolve(strict=True)
        if not binary.is_file():
            raise SystemExit("expected a native executable file")
        command = [str(binary)]
    try:
        print(json.dumps(asyncio.run(verify(command, not args.source))))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": "aipg.worker.local-lifecycle.v1",
                    "passed": False,
                    "failure_type": type(exc).__name__,
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
