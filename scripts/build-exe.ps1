# Build grid-inference-worker.exe on Windows
# Usage: scripts\build-exe.ps1              -> one file (portable, slower startup)
#        scripts\build-exe.ps1 -OneDir      -> folder (faster startup, no extraction)
# Output: dist\grid-inference-worker.exe  OR  dist\grid-inference-worker\grid-inference-worker.exe

param([switch]$OneDir)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

# Prefer venv
if (Test-Path ".venv\Scripts\Activate.ps1") {
    & .\.venv\Scripts\Activate.ps1
} elseif (Test-Path "venv\Scripts\Activate.ps1") {
    & .\venv\Scripts\Activate.ps1
}

Write-Host "Installing package and PyInstaller..." -ForegroundColor Cyan
pip install -e . -q
pip install pyinstaller -q
pip install pillow -q

# Generate icons (make_icon.py). Use root favicon.ico for exe + taskbar (sharp).
python scripts/make_icon.py
$iconPath = Join-Path $repoRoot "favicon.ico"
$iconArg = if (Test-Path $iconPath) { @("--icon", (Resolve-Path $iconPath).Path) } else { @() }

$templates = "inference_worker/web/templates;inference_worker/web/templates"
$static    = "inference_worker/web/static;inference_worker/web/static"
$certs     = "inference_worker/certs;inference_worker/certs"
$faviconData = if (Test-Path $iconPath) { "favicon.ico;." } else { "" }

$mode = if ($OneDir) { "directory (fast startup)" } else { "single file" }
Write-Host "Building EXE ($mode)..." -ForegroundColor Cyan

# Remove old output so PyInstaller doesn't hit "Access is denied" if the exe is still running.
$exePath = Join-Path $repoRoot "dist\grid-inference-worker.exe"
$dirPath = Join-Path $repoRoot "dist\grid-inference-worker"
try {
  if (Test-Path $exePath) { Remove-Item $exePath -Force }
  if (Test-Path $dirPath) { Remove-Item $dirPath -Recurse -Force }
} catch {
  Write-Host "ERROR: Could not remove old build (close grid-inference-worker.exe if it's running, then try again)." -ForegroundColor Red
  exit 1
}

# One-file: use spec so app.manifest (DPI-aware) is embedded. Onedir: use CLI.
if (-not $OneDir) {
  python -m PyInstaller --noconfirm --clean grid-inference-worker.spec
} else {
  $addDataArgs = @("--add-data", $templates, "--add-data", $static, "--add-data", $certs)
  if ($faviconData) { $addDataArgs += "--add-data", $faviconData }
  $manifestPath = Join-Path $PSScriptRoot "app.manifest"
  $manifestArg = if (Test-Path $manifestPath) { @("--manifest", (Resolve-Path $manifestPath).Path) } else { @() }
  $pyiArgs = @(
    "--noconfirm"
    "--noconsole"
    "--noupx"
    "--clean"
    "--name", "grid-inference-worker"
    , $iconArg
    , $manifestArg
    , $addDataArgs
    "--hidden-import", "inference_worker.env_utils"
    "--hidden-import", "inference_worker.service"
    "--hidden-import", "inference_worker.gui"
    "--hidden-import", "inference_worker.headless"
    "--hidden-import", "inference_worker.web.routes"
    "--hidden-import", "python_multipart"
    "--hidden-import", "multipart"
    "--hidden-import", "eth_account"
    "--hidden-import", "eth_account.messages"
    "--copy-metadata", "py_ecc"
    "--hidden-import", "uvicorn.logging"
    "--hidden-import", "uvicorn.loops.auto"
    "--hidden-import", "uvicorn.protocols.http.auto"
    "--hidden-import", "uvicorn.protocols.websockets.auto"
    "--hidden-import", "uvicorn.lifespan.on"
    "--hidden-import", "uvicorn.lifespan.off"
    "run_frozen.py"
  )
  python -m PyInstaller --onedir @pyiArgs
}

if ($OneDir) {
    if (Test-Path "dist\grid-inference-worker\grid-inference-worker.exe") {
        Write-Host "Done. Run: dist\grid-inference-worker\grid-inference-worker.exe (fast startup)" -ForegroundColor Green
    } else {
        Write-Host "Build failed. Check output above." -ForegroundColor Red
        exit 1
    }
} else {
    if (Test-Path "dist\grid-inference-worker.exe") {
        Write-Host "Done. EXE: dist\grid-inference-worker.exe" -ForegroundColor Green
    } else {
        Write-Host "Build failed. Check output above." -ForegroundColor Red
        exit 1
    }
}
