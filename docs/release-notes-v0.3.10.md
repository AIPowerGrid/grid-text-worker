# Text Worker v0.3.10

This release makes the worker practical to set up on a headless Linux server
and fixes vision detection for reasoning models. Job execution, charging, den
accounting, and payout settlement are unchanged from v0.3.9.

## Operator Changes

- **Terminal setup.** `grid-inference-worker --setup` configures the worker
  without a browser. With `--backend-url` it runs without prompts: it reads the
  served model from the backend, proves it with a live completion, and saves
  the same configuration as the browser wizard. `--concurrency`, `--grid-model`,
  `--backend-api-key`, and `--install-service` combine with it. One connection
  without `--api-key` uses the secure Console approval link; parallel slots
  still need an advanced account key.
- **Linux service install over SSH.** `--install-service` now uses `sudo` in a
  terminal and the desktop `pkexec` prompt only when a desktop session exists.
  If neither works it prints the systemd unit and the commands to install it by
  hand, instead of failing with "Authentication cancelled".
- **Backend URLs** may be entered with or without a trailing `/v1` in the
  wizard, command-line flags, and detection.
- **Vision detection** reads both `reasoning` and `reasoning_content`, gives
  reasoning models room to answer, asks templates that support it to skip
  thinking, and runs once per backend for all parallel connections, so one
  model can no longer advertise mixed text-only and vision capability.
- **LM Studio** context limits come from the loaded model instance rather than
  the catalog entry (#39).

## Release Qualification

Not yet qualified. CI builds and verifies the four-platform payload for the
tagged commit; supervised production qualification must be recorded here
before publication.

## Platform Trust

- Linux x64 and ARM64 are the only platforms exposed by the public `/run`
  release gate after the complete release envelope verifies.
- Windows is unsigned and macOS is not notarized. Their manifest state and
  installation warnings remain explicit; signing is recommended but not a
  publication blocker.
- Windows may show a SmartScreen warning. macOS Gatekeeper may require an
  explicit open/allow action. Verify the published checksum and provenance
  before running either download; do not disable OS protections globally.
