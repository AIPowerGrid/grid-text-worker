# Text Worker Tests

## Purpose

Fast pytest coverage for pure backend-detection and utility behavior. The suite
does not currently prove a live backend-to-Grid job lifecycle.

## Ownership

- `test_smoke.py` - known-engine table, OpenAI model parsing, header detection,
  platform helpers, and backend URL policy.
- `test_web_security.py` - management-plane redirect and backend-persistence
  boundaries.
- `test_release_assets.py` - offline release payload, checksum, and archive
  safety verification.

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
- For transport changes, perform the manual worker lifecycle in the parent
  guide in addition to this suite.

## Child DOX Index

No child guides are currently required; this file owns `tests/`.
