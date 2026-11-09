# Grid Text Worker v0.3.12

Includes the operator fixes documented in `release-notes-v0.3.11.md`. That
candidate was never published: downloaded-archive testing found that Python ZIP
creation expanded macOS framework symlinks into ordinary files. The executable
ran, but the app bundle failed signature-integrity validation.

## Packaging Correction

macOS packaging now preserves symlinks with `ditto`, extracts the completed ZIP
into a fresh directory, verifies bundle signature integrity before and after
packing, and runs the native lifecycle fixture on the extracted app. Linux and
Windows run that fixture against the staged download file, not the build input.

## Operator Changes Since v0.3.10

- Bounded generation read timeout defaults to 600 seconds instead of 120.
  `GRID_BACKEND_READ_TIMEOUT` accepts 10-3600 seconds. Restart after editing.
  A timeout fails the job but retains availability when the health probe passes.
- Terminal results follow backend/cancel-reader cleanup so the next assignment
  cannot be consumed by the previous reader. Received cancellation closes pending
  HTTP work and retains only observed partial output and usage.
- Ollama advertises loaded context from `/api/ps`; unknown capacity falls back
  to at most 4096. Operator context caps still apply.
- Expired Console enrollment can recover a changed worker name without replacing
  its rig signer. Active enrollment remains exact-name and API-bound.
- Docker publishes a usable host-loopback dashboard and checks health correctly.
- Settings expose automatic-context reset and the generation read timeout.
- Patched certifi, Pillow and setuptools remain locked.
- `--no-gui --no-browser` supports unattended console launch.

## Upgrade And Trust

Stop the old worker, replace only its executable/app, then restart. Keep existing
configuration and the rig signer; no new account, key or worker identity is needed.
Verify the manifest, checksums and GitHub workflow attestations. The Linux
installer installs without running the worker or collecting credentials.

Windows is unsigned and may show SmartScreen warnings. macOS is ad-hoc signed
and not notarized; Gatekeeper may require per-app approval. Bundle integrity is
checked, but it is not a verified publisher identity. Do not disable OS security
globally. The manifest discloses the platform-signing state explicitly.

## Qualification Limits

All four native platforms must pass runtime/CA checks and synthetic loopback
tests: real HTTP read timeouts, immediate next-job completion and received cancel
closing pending HTTP sockets, across five protocol variants. The dashboard must
reject unauthenticated status access. Publication also requires exact downloaded
payload/provenance verification and supervised native staging.

This is not real Core/GPU, eight-minute prefill, enrollment, billing or payout
qualification. Core cancellation-forwarding latency is outside this worker test.
No model weights, prices, earning policy or production backend is changed.
