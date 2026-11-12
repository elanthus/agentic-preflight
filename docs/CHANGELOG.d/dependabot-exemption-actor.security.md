- The hosted Dependabot approval exemption and the Dependabot auto-merge and attestation
  exception now also require that `dependabot[bot]` set the current head. Signed bot
  commit metadata shows only that GitHub created a commit. The run's event must be a
  `pull_request_target` `opened` or `synchronize` event for the exact head, sent by the
  `dependabot[bot]` Bot account; `.github/scripts/verify_dependabot.py` takes the new
  required `--event-name` and `--event-file` arguments and reports `head_set_by_other_actor`,
  `head_actor_unknown`, or `event_mismatch` when it fails closed. A head that a person
  pushed, including a clean **Update branch** merge, and runs from `reopened`,
  `ready_for_review`, or auto-merge events now need owner approval or a Dependabot rebase.
  `docs/limits.md` describes the exemption's guarantee. (GHSA-v55v-mgfw-4j8q)
