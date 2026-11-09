# Grid Text Worker v0.3.11

## Operator fixes

- Long prefill no longer hits the old 120-second worker read timeout. The default
  is 600 seconds; `GRID_BACKEND_READ_TIMEOUT` accepts 10-3600 seconds. Connect
  and health-check deadlines stay short. A read timeout fails that job but keeps
  the backend available if its health probe passes. Restart after changing it.
- Timeout and HTTP-error cleanup finish before reporting the terminal result,
  so the previous cancellation reader cannot consume the next assignment.
  Received cancellation also aborts silent/pre-header HTTP requests and retains
  only observed partial output and usage.
- Ollama advertises loaded `/api/ps` context instead of architectural capacity.
  Unknown loaded capacity falls back to at most 4096; operator caps still apply.
- An expired pending Console enrollment can be retried with a changed worker
  name. Active enrollment remains exact-name and API-bound; keep existing identity.
- Docker binds inside the container while publishing the dashboard on host
  loopback, with a working authenticated-management-plane health check.
- Settings show/reset automatic context and expose the generation read timeout.
- Patched certifi, Pillow and setuptools are pinned in the frozen lock.
- `--no-gui --no-browser` supports unattended console launch without opening a
  browser. Dashboard authentication and loopback defaults are unchanged.

## Upgrade

Stop the old worker, replace its executable with the matching release asset,
then start it normally. Preserve the existing configuration and rig signer;
do not create another account, key or worker identity just to upgrade.

Inspect the release manifest, SHA256SUMS and GitHub workflow attestations before
installing. The Linux installer is checksummed and release-bound; it installs
without running the worker or collecting credentials.

## Qualification And Limits

The frozen four-platform build must pass the complete Python/UI suite, runtime
and CA checks, then an actual-executable lifecycle rehearsal against synthetic
loopback peers. For Chat plus streaming/nonstreaming Responses and Messages,
that rehearsal forces a real HTTP read timeout, immediately dispatches another
job, verifies its output, then checks that received cancellation closes a pending
HTTP socket. The unauthenticated dashboard must return 401. Disposable fixture
configuration and signing state never enter release artifacts.

This is not a live Core/GPU, eight-minute prefill, account enrollment, billing
or payout qualification. It does not establish how quickly Core forwards client
cancellation. No model weights, pricing, earning rules or public API policy change.

## Platform Trust

Windows is unsigned: SmartScreen may warn. macOS is ad-hoc signed and not
notarized: Gatekeeper may block opening. Verify the exact release and use the
OS per-app approval flow only for a trusted download; do not disable platform
security globally. The manifest discloses these limits explicitly. Publication
still requires supervised checks of the exact immutable candidate payload.
