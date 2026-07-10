# Text Worker Build Scripts

## Purpose

Windows executable packaging, manifests, launch helpers, and generated icon or
splash assets for the distributable text worker.

## Ownership

- `build-exe.ps1`, `app.manifest` - Windows/PyInstaller packaging.
- `run.ps1` - local Windows launch helper.
- `make_icon.py`, `make_splash.py` - deterministic build-asset generation.

## Local Contracts

- Release artifacts must not embed `.env`, API keys, payout wallets, local
  paths, signing material, or backend credentials.
- Keep package entrypoints and data-file inclusion aligned with the Python CLI,
  local web UI, version metadata, and release workflow.
- Generated artwork must use repository-owned source assets and remain
  reproducible from the scripts.

## Work Guidance

- Treat Windows quoting, path, architecture, and code-signing behavior as part
  of the release contract.
- Update release docs and workflow asset names with packaging changes.

## Verification

- Build on the target Windows architecture and run `--help`, setup, backend
  detection, and one local dashboard boot from outside the source checkout.
- Inspect the packaged file list and executable strings for secrets and local
  absolute paths.

## Child DOX Index

No child guides are currently required; this file owns `scripts/`.
