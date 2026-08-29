# Text Worker v0.3.6 Candidate

Status: release candidate only. No v0.3.6 tag or public release has been
created.

## Operator Changes

- The setup wizard waits for Core to accept every configured worker connection.
  A running local process or one healthy connection no longer makes a partial
  multi-backend worker appear online.
- Rejected, disconnected, partially connected, and local-process failures are
  visible separately. Session work and den are aggregated across connections.
- API-key links now open the live Console. Payout-wallet management is correctly
  described as account-owned and no longer appears as a local worker setting.
- Backend credentials remain editable after clearing them, and endpoint labels
  cover any OpenAI-compatible backend.
- Ollama model tests use the supported reasoning control. Empty visible output
  is an explicit failure instead of a successful test.
- The desktop manager can explicitly copy an authenticated local-dashboard
  link. Normal startup logs print only the token-free URL, and browser bootstrap
  removes the token from history after setting the local authentication cookie.

## Packaging And Security

- `pyproject.toml` and `uv.lock` now include the dashboard form parser and
  receipt-signing runtime. The duplicate `requirements.txt` was removed.
- Every platform build performs a real EIP-191 sign/recover self-test and checks
  the reviewed WebSocket CA inside the frozen artifact.
- CI tests real login submission, safe redirects, strict auth cookies, status
  truthfulness, multi-connection aggregation, and frozen dependency presence.

## Platform Trust

- Windows binaries are currently unsigned. Windows may show a SmartScreen
  warning; operators must verify the published checksum before running them.
- macOS binaries are currently not notarized. Gatekeeper may require an explicit
  open/allow action; operators must verify the published checksum first.
- Linux and macOS ARM64 artifacts and Windows x64 artifacts are built from the
  locked dependency graph. A public release must include checksums, SBOM,
  provenance, and the platform-signing manifest.

## Remaining Publication Gates

- Complete one native interactive Windows onboarding pass from an extracted
  candidate outside the source checkout.
- Complete one supervised production Grid job with the exact candidate and
  confirm the signed receipt and truthful disconnect/reconnect behavior.
- Present the final release payload and test evidence before publishing the
  draft. Published v0.3.5 remains immutable.
