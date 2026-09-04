# Text Worker v0.3.9

This candidate adds an authoritative worker-status view and a Core-routed
connectivity canary to the local manager. Ordinary customer job execution,
charging, den accounting, and payout settlement are unchanged from v0.3.8.

## Operator Changes

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

## Candidate Gates

Publication remains blocked until:

1. The matching Core status and canary endpoints are deployed and their
   worker-credential boundaries pass a supervised production check.
2. The exact frozen Linux candidate displays authoritative status and passes
   the hard-targeted connectivity canary while its worker is connected.
3. The complete release envelope, platform builds, runtime self-checks,
   browser tests, secret scans, SBOM, checksums, and provenance attestations
   pass for the immutable tag.

## Platform Trust

- Linux x64 and ARM64 are the only platforms exposed by the public `/run`
  release gate after the complete release envelope verifies.
- Windows is unsigned and macOS is not notarized. Their manifest state and
  installation warnings remain explicit; signing is recommended but not a
  publication blocker.
