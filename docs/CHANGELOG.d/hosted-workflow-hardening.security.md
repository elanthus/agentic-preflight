- The `high-risk human approval` job now passes without an approval only when the policy
  job reports exactly `false` for `requires_approval`. An empty, `null`, or other value
  fails the job instead of being read as "no approval required".
