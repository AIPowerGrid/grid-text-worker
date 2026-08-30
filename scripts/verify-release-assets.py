# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify the complete text-worker release payload offline."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

PAYLOADS = (
    "grid-inference-worker-linux-x64",
    "grid-inference-worker-linux-arm64",
    "grid-inference-worker-macos-arm64.zip",
    "grid-inference-worker-windows-x64.exe",
    "grid-inference-worker-release.spdx.json",
    "install-worker.sh",
)
CHECKSUMMED = (*PAYLOADS, "worker-release.json")


def _die(message: str) -> None:
    raise SystemExit(f"error: {message}")


def verify(root: Path) -> None:
    if not root.is_dir():
        _die(f"release directory not found: {root}")
    for name in (*CHECKSUMMED, "SHA256SUMS"):
        if not (root / name).is_file():
            _die(f"missing release asset: {name}")

    entries: dict[str, str] = {}
    for raw in (root / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        parts = raw.split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            _die(f"invalid SHA256SUMS line: {raw!r}")
        name = parts[1].lstrip("*")
        if name in entries:
            _die(f"duplicate checksum entry: {name}")
        entries[name] = parts[0].lower()
    if set(entries) != set(CHECKSUMMED):
        _die("SHA256SUMS does not exactly match the release payload")
    for name, expected in entries.items():
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != expected:
            _die(f"checksum mismatch: {name}")

    manifest = json.loads((root / "worker-release.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "aipg-text-worker-release-v1":
        _die("release manifest schema is invalid")
    version = str(manifest.get("version") or "")
    tag = str(manifest.get("tag") or "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        _die("release version is invalid")
    if tag and tag.removeprefix("v") != version:
        _die("release tag does not match version")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("commit") or "")):
        _die("release commit is invalid")
    signing = manifest.get("platform_signing")
    if not isinstance(signing, dict) or set(signing) != {"macos", "windows"}:
        _die("release platform-signing state is invalid")
    macos = signing.get("macos")
    windows = signing.get("windows")
    if (
        not isinstance(macos, dict)
        or set(macos) != {"verified", "identity", "notarized", "team_id"}
        or not isinstance(macos.get("verified"), bool)
        or not isinstance(macos.get("notarized"), bool)
        or not isinstance(macos.get("identity"), str)
        or (macos.get("team_id") is not None and not isinstance(macos["team_id"], str))
    ):
        _die("macOS signing state is invalid")
    if (
        not isinstance(windows, dict)
        or set(windows) != {"verified", "identity", "subject"}
        or not isinstance(windows.get("verified"), bool)
        or not isinstance(windows.get("identity"), str)
        or (
            windows.get("subject") is not None
            and not isinstance(windows["subject"], str)
        )
    ):
        _die("Windows signing state is invalid")
    assets = manifest.get("assets")
    if (
        not isinstance(assets, list)
        or not all(isinstance(item, dict) for item in assets)
        or {item.get("name") for item in assets} != set(PAYLOADS)
    ):
        _die("release manifest asset list is invalid")
    for item in assets:
        path = root / item["name"]
        if item.get("bytes") != path.stat().st_size:
            _die(f"release manifest size mismatch: {path.name}")
        if item.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            _die(f"release manifest checksum mismatch: {path.name}")

    if not (root / "grid-inference-worker-linux-x64").read_bytes().startswith(b"\x7fELF"):
        _die("linux x64 asset is not ELF")
    if not (root / "grid-inference-worker-linux-arm64").read_bytes().startswith(b"\x7fELF"):
        _die("linux arm64 asset is not ELF")
    if not (root / "grid-inference-worker-windows-x64.exe").read_bytes().startswith(b"MZ"):
        _die("windows asset is not PE")

    with zipfile.ZipFile(root / "grid-inference-worker-macos-arm64.zip") as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if not names:
        _die("macOS archive is empty")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            _die("macOS archive contains an unsafe path")
    expected_suffix = "Grid Inference Worker.app/Contents/MacOS/grid-inference-worker"
    if not any(name.endswith(expected_suffix) for name in names):
        _die("macOS archive does not contain the worker executable")

    sbom = json.loads(
        (root / "grid-inference-worker-release.spdx.json").read_text(encoding="utf-8"),
    )
    if not str(sbom.get("spdxVersion") or "").startswith("SPDX-"):
        _die("release SBOM is not SPDX JSON")

    installer = (root / "install-worker.sh").read_text(encoding="utf-8")
    installer_tag = tag or f"v{version}"
    if (
        not installer.startswith("#!/usr/bin/env bash\n")
        or "__AIPG_WORKER_RELEASE_TAG__" in installer
        or installer.count(f'RELEASE_TAG="{installer_tag}"') != 1
        or "https://github.com/AIPowerGrid/grid-text-worker/releases/download/"
        not in installer
        or "sha256sum" not in installer
        or re.search(r"\|\s*(?:ba)?sh\b", installer)
    ):
        _die("Linux installer identity or safety contract is invalid")


def main() -> None:
    if len(sys.argv) != 2:
        _die("usage: verify-release-assets.py <release-directory>")
    verify(Path(sys.argv[1]))
    print(f"Verified text-worker release payload in {sys.argv[1]}")


if __name__ == "__main__":
    main()
