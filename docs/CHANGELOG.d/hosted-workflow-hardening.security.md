- The `high-risk human approval` job now passes without an approval only when the policy
  job reports exactly `false` for `requires_approval`. An empty, `null`, or other value
  fails the job instead of being read as "no approval required".
- Every `actions/checkout` step in a `pull_request_target` workflow sets
  `persist-credentials: false`, so the workflow token is not left in the checkout's Git
  configuration. A governance test keeps new checkouts in those workflows the same.
- `init` replaces a symlink at the pre-push hook path instead of writing through it.
  A dangling link no longer creates its target, and `--force` no longer overwrites the
  file a link points to, which could be a tracked file when `core.hooksPath` points
  into the repository. `data.hook_path` now names the link rather than its target.
