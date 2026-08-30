"""End-to-end checks for the non-executing Linux release installer."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

INSTALLER = Path(__file__).parents[1] / "scripts" / "install-worker.sh"


def test_installer_source_has_one_fixed_release_identity() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert source.count("__AIPG_WORKER_RELEASE_TAG__") == 1
    assert source.count(
        "https://github.com/AIPowerGrid/grid-text-worker/releases/download/"
    ) == 1
    assert "AIPG_WORKER_RELEASE_ROOT" not in source
    assert "| sh" not in source
    assert "| bash" not in source


def _fake_release(
    root: Path,
    binary: bytes,
    asset: str = "grid-inference-worker-linux-x64",
) -> None:
    (root / asset).write_bytes(binary)
    manifest = b'{"schema":"aipg-text-worker-release-v1"}\n'
    (root / "worker-release.json").write_bytes(manifest)
    rows = []
    for name in (asset, "worker-release.json"):
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        rows.append(f"{digest}  {name}")
    (root / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="ascii")


def _fake_commands(root: Path) -> Path:
    commands = root / "bin"
    commands.mkdir()
    curl = commands / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
output=""
url=""
while (($#)); do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
cp "$MOCK_RELEASE_DIR/${url##*/}" "$output"
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    uname = commands / "uname"
    uname.write_text(
        """#!/usr/bin/env bash
case "${1:-}" in
  -s) echo Linux ;;
  -m) echo "${MOCK_UNAME_ARCH:-x86_64}" ;;
  *) echo Linux ;;
esac
""",
        encoding="utf-8",
    )
    uname.chmod(0o755)
    sha256sum = commands / "sha256sum"
    sha256sum.write_text(
        f"""#!{sys.executable}
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
print(hashlib.sha256(path.read_bytes()).hexdigest(), path)
""",
        encoding="utf-8",
    )
    sha256sum.chmod(0o755)
    return commands


def _installer_env(
    tmp_path: Path,
    release: Path,
    arch: str = "x86_64",
) -> dict[str, str]:
    commands = _fake_commands(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{commands}:{env['PATH']}"
    env["MOCK_RELEASE_DIR"] = str(release)
    env["MOCK_UNAME_ARCH"] = arch
    return env


def test_installer_verifies_and_installs_without_running(tmp_path: Path) -> None:
    marker = tmp_path / "worker-ran"
    binary = f"#!/usr/bin/env bash\ntouch {marker}\n".encode()
    release = tmp_path / "release"
    release.mkdir()
    _fake_release(release, binary)
    target = tmp_path / "installed"

    result = subprocess.run(
        ["bash", str(INSTALLER), "--install-dir", str(target)],
        text=True,
        capture_output=True,
        env=_installer_env(tmp_path, release),
        check=False,
    )

    destination = target / "grid-inference-worker"
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == binary
    assert destination.stat().st_mode & stat.S_IXUSR
    assert not marker.exists()
    assert "never requested a Grid key or wallet secret" in result.stdout


def test_installer_selects_the_arm64_release_asset(tmp_path: Path) -> None:
    binary = b"arm64-worker"
    release = tmp_path / "release"
    release.mkdir()
    _fake_release(release, binary, "grid-inference-worker-linux-arm64")
    target = tmp_path / "installed"

    result = subprocess.run(
        ["bash", str(INSTALLER), "--install-dir", str(target)],
        text=True,
        capture_output=True,
        env=_installer_env(tmp_path, release, "aarch64"),
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (target / "grid-inference-worker").read_bytes() == binary


def test_installer_rejects_a_tampered_binary(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    _fake_release(release, b"original")
    (release / "grid-inference-worker-linux-x64").write_bytes(b"tampered")
    target = tmp_path / "installed"

    result = subprocess.run(
        ["bash", str(INSTALLER), "--install-dir", str(target)],
        text=True,
        capture_output=True,
        env=_installer_env(tmp_path, release),
        check=False,
    )

    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (target / "grid-inference-worker").exists()
