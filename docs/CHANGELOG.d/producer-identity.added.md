- Record producer identity on every new run: the Agentic Preflight version, the Python
  version, the platform, the coding-agent client, and the model that the agent reports.
  `start` accepts `--client NAME` and `--model NAME`. Flags win over detection, and
  `CLAUDECODE=1` is the only detected client marker. Unknown values stay `null`, and the
  model is never inferred. `start`, single-run `status`, and the `run_created` event
  return the `producer` object. It is informational only and is not in the attestation,
  fingerprints, or configuration digests.
- Instruct the skill to pass `--client` and, when the agent knows its exact model id,
  `--model` on `start`.
