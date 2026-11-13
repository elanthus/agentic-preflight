- The `high-risk human approval` job now passes without an approval only when the policy
  job reports exactly `false` for `requires_approval`. An empty, `null`, or other value
  fails the job instead of being read as "no approval required".
- Every `actions/checkout` step in a `pull_request_target` workflow sets
  `persist-credentials: false`, so the workflow token is not left in the checkout's Git
  configuration. A governance test keeps new checkouts in those workflows the same.
- `init` replaces a symlink at the pre-push hook path instead of writing through it.
  A dangling link no longer creates its target, and `--force` no longer overwrites the
  file a link points to, which could be a tracked file when `core.hooksPath` points
  into the repository. A link to exactly the current hook script is replaced with a
  regular file too. `data.hook_path` now names the link rather than its target, and a
  `core.hooksPath` that ends in whitespace is kept as configured.
- Review grounding reads `docs/**`, `AGENTS.md`, `CLAUDE.md`, `[context] extra_paths`,
  and the `[context]` settings themselves from the run's merge base instead of the branch
  under review. A change cannot rewrite the conventions shown to its own reviewer, or turn
  grounding off or shrink it for its own review; a `[context]` change takes effect after
  it merges. The branch's edits to those files, including files it adds, reach the
  reviewer through the diff. For a branch that edits those files, the grounding changes,
  so its earlier review evidence is not reused.
- `human-approval-rerun.yml` re-runs the approval policy only when the user who submitted,
  edited, or dismissed the review is an owner, member, or collaborator with a review on
  the pull request, or has write access. A bot account, such as a GitHub App, re-runs it
  once it has dismissed an owner, member, or collaborator review of the pull request.
  When a lookup that decides authorization fails, the policy is re-run, with a warning,
  rather than skipped, so a stale success cannot stay authoritative. Comment reviews from other users on a public pull request no longer queue
  policy re-runs. Eligibility is decided in an `authorize`
  job before the re-run job joins its concurrency group, which is keyed by the pull
  request and its head, so an ineligible review, or a re-run for another pull request
  with the same head, cannot replace an eligible re-run that is waiting in that group.
- The configuration that `init` writes, and the Claude and Codex reviewer example
  configurations, add `docs/CODEOWNERS` and the reviewer wrapper directory
  `docs/examples/reviewers/**` to `human_review_paths`. Existing configuration files are
  not changed; add these paths by hand to apply them.
