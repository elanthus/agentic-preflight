- Use ASD-STE100 style in the remaining user documentation: installation, configuration,
  worktree modes, the pre-push hook, attestations and CI enforcement, limits, the 0.7
  upgrade guide, the fingerprint contract, grounded context, change scope, the state
  machine, prior art, both ADRs, and the support, security, compatibility, contributing,
  and releasing guides. The state-machine guide now lists every declared transition, and
  a test keeps that table equal to `machine.py`. The demo fixture warms the `uvx` caches
  before a recording, and the tape waits longer after `mergeback`. Behavior and
  configuration are unchanged.
