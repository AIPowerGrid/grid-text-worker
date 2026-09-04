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

The exact frozen Linux candidate must repeat backend detection, model
enumeration, runtime verification, and generation before this draft is
published. A running process or a successful local model response alone does
not prove Grid registration.

## Platform Trust

- Linux x64 and ARM64 remain the only platforms exposed by the public `/run`
  release gate after the complete release envelope verifies.
- Windows is unsigned and macOS is not notarized. Their manifest state and
  installation warnings remain explicit; signing is recommended but not a
  publication blocker.
