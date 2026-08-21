"""Release-payload integrity and archive-safety tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "verify-release-assets.py"
SPEC = importlib.util.spec_from_file_location("verify_release_assets", SCRIPT)
assert SPEC and SPEC.loader
release_verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_verifier)


def _write_payload(root: Path, mac_member: str | None = None) -> None:
    mac_member = mac_member or (
        "Grid Inference Worker.app/Contents/MacOS/grid-inference-worker"
    )
    payloads = {
        "grid-inference-worker-linux-x64": b"\x7fELF-x64",
        "grid-inference-worker-linux-arm64": b"\x7fELF-arm64",
        "grid-inference-worker-windows-x64.exe": b"MZ-windows",
        "grid-inference-worker-release.spdx.json": json.dumps(
            {"spdxVersion": "SPDX-2.3"}
        ).encode(),
    }
    for name, content in payloads.items():
        (root / name).write_bytes(content)

    mac_archive = root / "grid-inference-worker-macos-arm64.zip"
    with zipfile.ZipFile(mac_archive, "w") as archive:
        archive.writestr(mac_member, b"Mach-O")

    manifest_assets = []
    for name in release_verifier.PAYLOADS:
        path = root / name
        manifest_assets.append(
            {
                "name": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        )
    manifest = {
        "schema": "aipg-text-worker-release-v1",
        "tag": "v0.3.5",
        "version": "0.3.5",
        "commit": "a" * 40,
        "platform_signing": {
            "macos": {
                "verified": False,
                "identity": "adhoc",
                "notarized": False,
                "team_id": None,
            },
            "windows": {
                "verified": False,
                "identity": "unsigned",
                "subject": None,
            },
        },
        "assets": manifest_assets,
    }
    manifest_path = root / "worker-release.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    checksum_lines = []
    for name in release_verifier.CHECKSUMMED:
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        checksum_lines.append(f"{digest}  {name}")
    (root / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="ascii",
    )


def test_complete_release_payload_verifies(tmp_path: Path) -> None:
    _write_payload(tmp_path)

    release_verifier.verify(tmp_path)


def test_tampered_binary_is_rejected(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    (tmp_path / "grid-inference-worker-linux-x64").write_bytes(b"\x7fELF-tampered")

    with pytest.raises(SystemExit, match="checksum mismatch"):
        release_verifier.verify(tmp_path)


def test_unsafe_macos_archive_path_is_rejected(tmp_path: Path) -> None:
    _write_payload(tmp_path, "../Grid Inference Worker.app/Contents/MacOS/grid-inference-worker")

    with pytest.raises(SystemExit, match="unsafe path"):
        release_verifier.verify(tmp_path)


def test_missing_platform_signing_state_is_rejected(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    manifest_path = tmp_path / "worker-release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("platform_signing")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    checksum_lines = []
    for name in release_verifier.CHECKSUMMED:
        digest = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        checksum_lines.append(f"{digest}  {name}")
    (tmp_path / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="ascii",
    )

    with pytest.raises(SystemExit, match="platform-signing state"):
        release_verifier.verify(tmp_path)
