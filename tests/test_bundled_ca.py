# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip(
    "PyInstaller", reason="bundle verification requires the build extra"
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def verifier():
    return runpy.run_path(str(ROOT / "scripts/verify-bundled-ca.py"))["verify"]


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_onefile_certificate_is_exact_and_loadable(verifier, separator, monkeypatch):
    resource = verifier.__globals__["RESOURCE"]
    certificate = (ROOT / resource).read_bytes()
    archive = SimpleNamespace(
        toc={resource.replace("/", separator): ()}, extract=lambda _: certificate
    )
    monkeypatch.setitem(verifier.__globals__, "CArchiveReader", lambda _: archive)
    verifier(Path("unused-binary"))


def test_missing_certificate_fails_build(verifier, tmp_path, monkeypatch):
    monkeypatch.setitem(
        verifier.__globals__, "CArchiveReader", lambda _: SimpleNamespace(toc={})
    )
    with pytest.raises(SystemExit, match="missing"):
        verifier(tmp_path / "worker")


def test_different_certificate_fails_build(verifier, monkeypatch):
    archive = SimpleNamespace(
        toc={verifier.__globals__["RESOURCE"]: ()}, extract=lambda _: b"wrong"
    )
    monkeypatch.setitem(verifier.__globals__, "CArchiveReader", lambda _: archive)
    with pytest.raises(SystemExit, match="differs"):
        verifier(Path("unused-binary"))


@pytest.mark.parametrize("macos", [False, True])
def test_onedir_certificate(verifier, tmp_path, monkeypatch, macos):
    resource = verifier.__globals__["RESOURCE"]
    binary = tmp_path / "Contents/MacOS/worker" if macos else tmp_path / "worker"
    root = tmp_path / "Contents/Resources" if macos else tmp_path / "_internal"
    target = root / resource
    target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / resource).read_bytes())
    monkeypatch.setitem(
        verifier.__globals__, "CArchiveReader", lambda _: SimpleNamespace(toc={})
    )
    verifier(binary)
