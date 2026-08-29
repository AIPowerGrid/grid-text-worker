# -*- mode: python ; coding: utf-8 -*-
# One-file build. Run make_icon.py first. Icon + manifest use paths relative to spec.
# For onedir build use: .\build-exe.ps1 -OneDir

import os
from PyInstaller.utils.hooks import copy_metadata

spec_dir = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    ['run_frozen.py'],
    pathex=[],
    binaries=[],
    datas=copy_metadata('py_ecc') + [
        ('inference_worker/web/templates', 'inference_worker/web/templates'),
        ('inference_worker/web/static', 'inference_worker/web/static'),
        ('inference_worker/certs', 'inference_worker/certs'),
        ('favicon.ico', '.'),
    ],
    hiddenimports=[
        'inference_worker.env_utils',
        'inference_worker.service',
        'inference_worker.gui',
        'inference_worker.headless',
        'inference_worker.web.routes',
        'python_multipart',
        'multipart',
        'eth_account',
        'eth_account.messages',
        'uvicorn.logging',
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    exclude_binaries=False,
    name='grid-inference-worker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(spec_dir, 'favicon.ico'),
    manifest=os.path.join(spec_dir, 'scripts', 'app.manifest'),
)
