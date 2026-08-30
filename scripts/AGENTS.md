# Text Worker Build Scripts

## Purpose

Windows executable packaging, manifests, launch helpers, and generated icon or
splash assets for the distributable text worker.

## Ownership

- `build-exe.ps1`, `app.manifest` - Windows/PyInstaller packaging.
- `run.ps1` - local Windows launch helper.
- `make_icon.py`, `make_splash.py` - deterministic build-asset generation.
- `verify-release-assets.py` - offline checksum, manifest, platform-signing
  state, binary-format, archive-path, and SPDX verification for the complete
  release payload.
- `verify-bundled-ca.py` - checks one-file and onedir/macOS binaries contain the
  exact reviewed public WebSocket CA certificate and that OpenSSL can load it.
- `install-worker.sh` - release-stamped Linux x64/ARM64 installer. It downloads
  from the fixed Grid worker release, verifies the binary and manifest against
  `SHA256SUMS`, and installs atomically without running or configuring it.

## Local Contracts

- Release artifacts must not embed `.env`, API keys, payout wallets, local
  paths, signing material, or backend credentials.
- The Linux installer must not accept credentials, auto-execute the downloaded
  worker, permit an alternate release host, or use a download-to-shell pattern.
- Keep package entrypoints and data-file inclusion aligned with the Python CLI,
  local web UI, version metadata, and release workflow.
- All build paths include `inference_worker/certs`. The public Cloudflare Origin
  CA is required by the default WebSocket endpoint; it is not secret material.
  Never replace this dependency with disabled TLS verification.
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
