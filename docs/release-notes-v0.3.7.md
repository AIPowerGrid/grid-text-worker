# Text Worker v0.3.7

This release adds secure Console-assisted enrollment for ordinary single-model
workers. The matching Core enrollment API and Console approval flow passed a
supervised production canary before the release tag was created.

## Operator Changes

- The release includes `install-worker.sh` for Linux x64/ARM64. It is stamped
  to the exact release tag, checksummed and listed in the release manifest,
  verifies downloads before an atomic non-root install, and never starts the
  worker or accepts credentials.
- A normal setup generates the worker's signing identity and candidate Grid key
  locally, then opens a short-lived Console approval URL.
- The generated Grid key never enters browser JavaScript or a Console response.
  Only its hash, bounded capabilities, and signed registration proof cross the
  enrollment API.
- Approved credentials are restricted to worker registration and dispatch for
  one exact worker name. They cannot submit inference, spend credits, read the
  account, create keys, or manage identities.
- Enrollment is crash-resumable, secrets are written atomically with owner-only
  permissions, and the browser receives a stable error instead of internal
  exception details.
- Existing multi-backend and parallel operators retain the advanced scoped-key
  path because one enrolled credential cannot impersonate several workers.
- Dashboard Den/hr and Jobs/hr rates are rounded to one decimal place and
  captured once per page load, so the cards stay readable instead of drifting
  every few seconds as session uptime changes.

## Release Evidence

The release was gated on all of the following:

1. The exact reviewed Core release containing worker enrollment is deployed.
2. The matching Console approval routes are deployed without exposing service
   credentials or generated worker keys.
3. One disposable worker completes browser approval, receives the locally held
   credential, connects with the exact approved name, and appears in the public
   worker registry.
4. Denial, expiry, replay, wrong-name registration, and reconnect behavior are
   verified against production.
5. The four-platform build, runtime self-check, checksum manifest, SPDX SBOM,
   and GitHub provenance checks pass at the tagged commit.

The production canary exercised 19 checks. It proved valid enrollment and
reconnect behavior while also rejecting the wrong poll token, wrong payout
signature, wrong worker name, replayed registration proof, and a revoked key.
The disposable worker key was revoked after the registry check.

## Platform Trust

- Linux x64 and ARM64 remain the only platforms exposed by the public `/run`
  release gate while their complete release envelope verifies.
- Windows is unsigned and macOS is not notarized. Their manifest state and
  installation warnings remain explicit in this release.
