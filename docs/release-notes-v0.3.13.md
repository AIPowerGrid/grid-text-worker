# Grid Text Worker v0.3.13

One correctness fix on top of v0.3.12. Job execution, charging, den accounting
and payouts are unchanged.

## Result Signatures

Earlier releases signed each text result in a format Grid Core does not read,
so Core recorded every text job as unsigned. This release signs the same
output commitment Core computes and sends it in the field Core verifies
(`worker_sig`, an EIP-191 signature over `aipg-job:{job_id}:{result_hash}`).
The commitment covers visible text, and also reasoning and tool calls when
present.

Nothing changes for operators: the same rig signer is used, and a job whose
signature cannot be verified still settles as before. Passthrough jobs sent in
Anthropic Messages or OpenAI Responses format are not signed yet.

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

Not yet qualified. CI verifies the hash and signature against vectors generated
by Core's own functions, and signatures from this code were accepted by Core's
verification function in a local cross-check. No job from this release has yet
been confirmed as stored with a verified signature in the production ledger;
record that here before publication.
