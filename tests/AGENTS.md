# Text Worker Tests

## Purpose

Fast pytest coverage for pure backend-detection and utility behavior. The suite
does not currently prove a live backend-to-Grid job lifecycle.

## Ownership

- `test_smoke.py` - known-engine table, OpenAI model parsing, header detection,
  platform helpers, and backend URL policy.
- `test_web_security.py` - management-plane redirect and backend-persistence
  boundaries, real login form parsing/cookies, model-test response handling,
  dashboard-link clipboard behavior, and frozen-runtime dependency checks.
- `test_release_assets.py` - offline release payload, checksum, and archive
  safety verification.
- `test_worker_status.py` - mocked ready/rejected registration, disconnect and
  supervisor cleanup, multi-backend status, and credential-safe reporting.
- `test_enrollment.py` - Console device enrollment, private pending-state
  persistence, delegation verification, and capability-bound registration.
- `test_bundled_ca.py` - public CA inclusion checks for onefile and onedir
  layouts; requires the build extra. The release CI also checks actual binaries.
- `onboarding-ui.test.mjs` - Node built-in tests of the actual template scripts:
  bounded setup polling, rejection, unavailable status, and account links.
  It also keeps dashboard den labels on the work-accounting boundary and out of
  retired reward or earnings-rate language.

## Local Contracts

- Keep unit tests deterministic and free of live Grid/API credentials.
- Do not treat this smoke suite as proof of WebSocket reconnect, streaming,
  cancellation, max-token enforcement, payout identity, or service install.
- Add regression tests beside any pure behavior changed in
  `inference_worker/`.

## Work Guidance

- Mock network/backend boundaries narrowly; add a separate marked integration
  test when behavior requires Ollama/vLLM or a Grid endpoint.
- Test malformed and partial backend responses, not only happy paths.

## Verification

- Run `pytest -q` from the repository root.
- Run `node --test tests/onboarding-ui.test.mjs` with Node 20+ (CI runners include
  Node); these deterministic UI tests need no npm packages.
- For transport changes, perform the manual worker lifecycle in the parent
  guide in addition to this suite.

## Child DOX Index

No child guides are currently required; this file owns `tests/`.
