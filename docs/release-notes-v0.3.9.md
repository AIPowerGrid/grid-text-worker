# Text Worker v0.3.9

This release adds an authoritative worker-status view and a Core-routed
connectivity canary to the local manager. Ordinary customer job execution,
charging, den accounting, and payout settlement are unchanged from v0.3.8.

## Operator Changes

- Multi-model setup, dashboard pause/resume, and per-model Settings are now
  available. Multi-model rigs require an advanced account key; ordinary Console
  enrollment remains one exact worker name and one connection.
- Per-model Settings edit the effective endpoint, engine, credential, and
  concurrency. Existing secrets stay server-side and are not carried to a changed
  endpoint. Context limits cap detected backend capacity rather than replacing it.
- Roster schedules inherit the rig schedule unless explicitly overridden.
  Existing multi-window schedules and text/vision declarations survive edits;
  selecting no days pauses all week. These limits do not configure GPU memory.
- The dashboard reads Core's worker-scoped `GET /v1/workers/self` endpoint
  with the existing `worker.connect` credential.
- The response identifies only the credential-bound worker and its own jobs
  and den. Account-level payout information is limited to whether a wallet is
  configured, the latest lifecycle status, and the last paid time.
- The dashboard labels payout state as account-level and does not expose an
  account identity, balances, sibling workers, payout addresses, amounts,
  periods, or transaction hashes.
- Managers connected to an older Core release degrade gracefully instead of
  treating a missing endpoint as a worker failure.
- After secure Console enrollment registers the exact worker, setup asks Core
  to route one randomized exact-output request through that worker. Setup shows
  the worker as live only when the result is bound to the requested worker and
  Core reports no economic effect.
- The canary creates no customer charge, worker den, payout, strike, validator
  evidence, or quality score. It proves Grid connectivity and exact output,
  not model identity, intelligence, or general quality.
- Advanced account-key setups remain registration-confirmed because those
  credentials are not bound to one exact worker.

## Release Qualification (2026-09-08)

- Source: `1e5feb38d9ccc5ff94d4c08892f86a31fd48a65b` (reviewed PR #37).
  All 130 Python tests and 32 browser-script tests passed. The tagged workflow
  built all four platforms, checked frozen runtime dependencies, and assembled
  verified checksums, manifest, SBOM, and GitHub provenance attestations.
- The exact tagged Linux x64 binary ran on Ubuntu 24.04 against real local
  Ollama Qwen 3 1.7B. Its authenticated dashboard API returned authoritative Core
  self-status and redacted account payout status. The hard-targeted setup check
  passed in 10,971 ms with exact output and no economic effect.
- Core `d139835324fe9b05820b1ada1984fea0bb9fb538` is deployed. It includes
  a 512-token reasoning budget and an explicitly delimited public test label.
  Staging caught Ollama's template appending `/think` to an undelimited label;
  the wording was corrected, not the exact comparator. Two failed setup checks
  and the successful check each produced zero ledger rows or reservations.
- The unfunded fixture was operator-provisioned with a signed delegation and a
  two-hour `worker.connect` key. This did not repeat a human's Console wallet
  approval or test native Windows/macOS wallet UI. Those platforms have CI
  build/runtime checks, not the supervised Linux production proof.
- While connected before the bounded final check, the fixture also served two
  ordinary jobs, totaling 4.58 den with no billing reservations. Those are not
  setup-canary records. Their append-only history is preserved and their exact
  identifiers are retained in protected operations evidence for exclusion before
  payout resumption. Payout timers remained disabled throughout qualification.
- The isolated process was stopped, its key revoked and rejected with HTTP 401,
  and only its generated account's payout addresses cleared after verifying
  zero payout records. This does not remove its historical denominator weight;
  the protected reconciliation record remains mandatory before payout resumption.
  Backend protocol
  detection can take more than 90 seconds during a cold/busy reconnect; a
  running process alone is not successful registration or setup.

## Platform Trust

- Linux x64 and ARM64 are the only platforms exposed by the public `/run`
  release gate after the complete release envelope verifies.
- Windows is unsigned and macOS is not notarized. Their manifest state and
  installation warnings remain explicit; signing is recommended but not a
  publication blocker.
- Windows may show a SmartScreen warning. macOS Gatekeeper may require an
  explicit open/allow action. Verify the published checksum and provenance
  before running either download; do not disable OS protections globally.
