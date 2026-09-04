# Text Worker v0.3.9

This candidate adds an authoritative worker-status view to the local manager.
It does not change Grid job execution, charging, den accounting, payout
settlement, enrollment, or transport behavior from v0.3.8.

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

## Candidate Gates

Publication remains blocked until:

1. The matching Core endpoint is deployed and its worker-credential boundary
   passes a supervised production check.
2. The exact frozen Linux candidate displays authoritative status while its
   worker is connected to the Grid.
3. The complete release envelope, platform builds, runtime self-checks,
   browser tests, secret scans, SBOM, checksums, and provenance attestations
   pass for the immutable tag.

## Platform Trust

- Linux x64 and ARM64 are the only platforms exposed by the public `/run`
  release gate after the complete release envelope verifies.
- Windows is unsigned and macOS is not notarized. Their manifest state and
  installation warnings remain explicit; signing is recommended but not a
  publication blocker.
