# Text Worker v0.3.8

This release makes spare-capacity controls explicit for operators who connect
an existing Ollama, vLLM, SGLang, LMDeploy, LM Studio, or KoboldCpp backend.
It does not change the worker enrollment, payout, or Grid transport contracts
introduced in v0.3.7.

## Operator Changes

- Setup and Settings expose maximum parallel jobs as `GRID_MAX_THREADS`.
- Setup and Settings accept a bounded local-time capacity schedule. A schedule
  can pause the worker or lower its concurrency without changing the backend.
- Console-enrolled single-worker credentials remain restricted to one parallel
  Grid connection. Parallel operators must continue to use an advanced scoped
  Grid API key.
- Schedule JSON is schema-validated and canonicalized before it reaches the
  environment file. Unknown fields, invalid days or times, oversized payloads,
  and concurrency outside `0..16` are rejected.
- Dashboard settings now use an explicit field allowlist and reject multiline,
  oversized, malformed numeric, and unsupported values before persistence.

## Candidate Evidence

Before tagging, the source candidate passed:

1. 101 Python tests and 15 browser-onboarding tests.
2. The locked dependency check and the complete pull-request CI matrix.
3. Secret and infrastructure-string scans.
4. A real sidecar probe through the worker detector against Ollama 0.32.15 on
   an independently running GPU host. The detector enumerated both available
   models and an OpenAI-compatible generation completed with `finish_reason=stop`.

Before publication, the exact frozen Linux asset repeated its runtime
self-check, rendered the new capacity controls, detected and enumerated the
live Ollama backend, and completed a visible model response. It ran from an
isolated configuration directory beside the existing services and left no
process, credential, or test directory behind. A running process or a
successful local model response alone does not prove Grid registration; the
unchanged enrollment path retains the production evidence recorded for
v0.3.7.

## Platform Trust

- Linux x64 and ARM64 remain the only platforms exposed by the public `/run`
  release gate after the complete release envelope verifies.
- Windows is unsigned and macOS is not notarized. Their manifest state and
  installation warnings remain explicit; signing is recommended but not a
  publication blocker.

## Subsequent Owned Bridge Rollout

On September 5, an owned multi-backend production bridge was upgraded from an
older source checkout with local vision/cancellation changes to the v0.3.8
source tag `afbb37595b61fa92a3b5bbd4dbba7320d31e008d`. Review confirmed that the
local changes were already included in this release. The deployment uses a
separate source directory and virtual environment installed from the tag's
hash-locked dependencies, not the frozen executable. It preserves the prior
checkout for rollback and the existing headless `run_worker.py` launch mode.

The existing configuration, signer and original service unit were unchanged
under before/after digest checks; an additive executable override selects the
release while preserving the working directory. The chat frontend, API,
database and model servers were not restarted. All three configured workers
registered with Core, a subsequent DeepSeek completion was recorded, and the
public chat login remained available with HTTP 200.

On that VM, 95 tests passed with one skip. A separate real-GPT-OSS request
through the released handler emitted 29 native-logprob token frames and one
completion without error to an in-memory Grid-side receiver. This proves
native backend-to-bridge relay, not a live end-to-end validator assignment or
general model fidelity. The 32-token response exhausted its reasoning budget
without visible content.

The validator's
[real calibration report](https://github.com/AIPowerGrid/grid-validator/blob/docs/validator-real-fidelity-measurements/TEXT_FIDELITY_EXPERIMENT_2026_09_05.md)
records same-backend repeats, different-backend comparisons and successful
logprob-copying/probe-aware-routing evasions. Fidelity issuance and economic
authority remain off; worker logprob support does not make self-reported
probabilities trustworthy.
