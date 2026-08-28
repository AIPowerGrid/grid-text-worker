# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ensure frozen releases include the exact public WebSocket CA certificate."""

import ssl
import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

RESOURCE = "inference_worker/certs/cloudflare_origin_root.pem"


def verify(binary: Path) -> None:
    expected = (Path(__file__).resolve().parents[1] / RESOURCE).read_bytes()
    archive = CArchiveReader(str(binary))
    entry = next(
        (name for name in archive.toc if name.replace("\\", "/") == RESOURCE), None
    )
    if entry is not None:
        actual = archive.extract(entry)
    else:
        # Onedir builds keep data beside the executable; macOS uses Resources.
        roots = [binary.parent / "_internal"]
        if binary.parent.name == "MacOS":
            roots.append(binary.parent.parent / "Resources")
        resource = next(
            (root / RESOURCE for root in roots if (root / RESOURCE).is_file()), None
        )
        if resource is None:
            raise SystemExit(
                "error: frozen worker is missing its WebSocket CA certificate"
            )
        actual = resource.read_bytes()
    if actual != expected:
        raise SystemExit("error: bundled WebSocket CA differs from the reviewed source")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=actual.decode("ascii"))
    print("Bundled WebSocket CA verified (public certificate, not signing material)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify-bundled-ca.py BINARY")
    verify(Path(sys.argv[1]))
