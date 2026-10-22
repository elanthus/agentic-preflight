# Compatibility shim removal plan

Audience: maintainers. Status: proposal, 2026-10-22, written against `main` at `167546d`
(25 commits after the v0.7.0 tag of 2026-10-13).

## The rule this plan applies

A release reads data that the previous minor release wrote, and nothing older. Downgrade
is not supported: a newer release never bends its writes so that an older release can read
them. Every shim carries the release it is removed in, and a test fails when the package
version reaches that release while the shim still exists.

Under this rule, 0.8.0 must read 0.7.0 records, notes, and snapshots. It need not read
0.6.0 data, and it need not keep its own records readable by 0.7.0.

> **Schedule note, 2026-10-22.** 0.8.0 shipped the same day as stage 0, with nothing
> from stage 1. Every later release number below has therefore moved one minor later than
> the first draft: stage 1 lands in 0.9.0 and stage 2 in 0.10.0. The rule is unchanged.

## Inventory and schedule

| # | Shim | Introduced | Protects | Drop in |
| --- | --- | --- | --- | --- |
| 1 | `LegacyContractPlan`, `allow_legacy`, `previous_receipt` | after v0.7.0 | schema-1 receipts from unreleased dev builds | now |
| 2 | `omit_absent_producer` serializer | after v0.7.0 | 0.7.0 reading records 0.8.0 rewrote (downgrade) | now |
| 3 | `discard_legacy_rebased_tree` | 0.7.0 | 0.6.0 merge-back attempts | 0.9.0 |
| 4 | `retire_ci_delegation` and `telemetry._effective_state` | 0.7.0 | 0.6.0 delegated runs | 0.9.0 |
| 5 | `pending-update.json` handling (four sites) | 0.7.0 | 0.6.0 interrupted runs | 0.9.0, one line stays to 0.10.0 |
| 6 | `REMOVED_CONFIG_KEYS` entries removed in 0.6.0 | 0.6.0 | 0.5.x snapshots | 0.9.0 |
| 7 | `discard_retired_null_fields` on attestations | 0.7.0 | 0.6.0 verified notes | 0.10.0 |
| 8 | `REMOVED_CONFIG_KEYS` entries removed in 0.7.0 | 0.7.0 | 0.6.0 snapshots and config files | 0.10.0 |
| 9 | `LEGACY_DIRNAME`, `legacy_worktrees`, runner reclamation at the old root | 0.7.0 | worktrees created before 0.7.0 | 0.10.0 |
| 10 | `record.get("producer")` raw fallback in `telemetry._producer` | after v0.7.0 | 0.7.0 records without the key | 0.10.0 |

Run schema 2, attestation schema 7, and fingerprint version 2 do not change in any stage.
No stage rewrites saved data or deletes user work. Rejected records keep the existing
`RUN_READ_RECOVERY` behaviour: preserved on disk, ownership pointers untouched.

## Stage 0: before 0.8.0, no compatibility note

These guard data that no release ever wrote. Items 1 and 2 are removed in
elanthus/agentic-preflight#301, with a `docs/CHANGELOG.d/*.removed.md` fragment and a
`COMPATIBILITY.md` wording update but no new compatibility entry. Stage 0 is complete.

### 1. Schema-1 contract receipts

Contract checks are opt-in, and the schema-2 plan landed in #290 after the v0.7.0 tag.
Any schema-1 receipt on disk came from a development build.

- `agentic_preflight/contract_models.py`: delete `LegacyContractCheck`,
  `LegacyContractUnit`, `LegacyContractPlan`; fold their fields into `ContractCheck` and
  `ContractUnit`; make `ContractReceipt.schema_version` `Literal["2"]` and `plan` a plain
  `ContractPlan`; drop the discriminated union and the version-match validator.
- `agentic_preflight/runs/contracts.py`: delete the `allow_legacy` parameter of
  `receipt_for`, its two call sites, and the `previous_receipt` branch of
  `contract_context`. A receipt that fails validation raises `InvalidFindings` as it
  already does for any other unreadable receipt.
- `tests/test_contract_checks.py`: remove the schema-1 fixtures and the `previous_receipt`
  assertions.
- `docs/review-contracts.md` lines 150 to 152 and the "Opt-in contract-check evidence"
  paragraphs of `COMPATIBILITY.md` that describe schema-1 readability.

### 2. Producer omission on write

`omit_absent_producer` exists so 0.7.0 can read a record that 0.8.0 loaded and rewrote.
That is downgrade support, which the rule excludes.

- `agentic_preflight/models.py`: delete the `@model_serializer` on `RunDoc`. Records
  without a producer serialise with `"producer": null`.
- `tests/test_producer.py`: drop the tests that assert the key is absent after a rewrite.
  Keep the tests that load a record without the key, since that is item 10.
- `COMPATIBILITY.md`, "Producer identity on run records": remove the sentence "A rewrite of
  such a record does not add the key" and the warning that earlier versions report the
  record as unreadable, which is now the normal downgrade position.

### Also removed in stage 0: `StageRecord.omit_absent_start`

Same pattern as item 2, same introduction date, same reasoning. Two look-alike
serializers stay: `Finding.omit_absent_response_reason` and
`ReviewCoverage.omit_disabled_contracts`. Both models sit inside `OriginalExecution`,
whose digest `origin_sha256` is checked on every load, and inside attestation notes
that a separately pinned protected verifier reads. Removing either would change the
bytes of every existing 0.7.0 evidence record and note, which breaks the rule rather
than applying it. They are wire-shape decisions for attested data, not downgrade shims,
and belong with any future attestation schema bump.

## Stage 1: 0.9.0

Each item gets a `removed` changelog fragment and one line in `COMPATIBILITY.md` stating
that 0.9.0 reads 0.8.0 data only. The existing per-feature paragraphs in
`COMPATIBILITY.md` for these shims move to `docs/upgrade-0.7.md`, which is where a reader
on 0.6.0 looks.

### 3. `mergeback_attempt.rebased_tree`

Neither 0.7.0 nor 0.8.0 writes the field. Delete `discard_legacy_rebased_tree` from `MergebackAttempt`
in `agentic_preflight/models.py` and its test in `tests/test_store.py`. A 0.6.0 attempt
then fails with `extra_forbidden`, which `store.py` already reports as `invalid` with the
preserve-and-use-a-compatible-tool recovery.

### 4. CI delegation retirement

0.7.0 writes no `test_delegation` key and no `TEST_DELEGATED` or `PUBLICATION_READY` state,
and its `start`, like 0.8.0's, already orphaned any such run it met.

- `agentic_preflight/models.py`: delete `retire_ci_delegation`. The states are not in
  `State`, so such a record fails validation naturally.
- `agentic_preflight/telemetry.py`: delete `_RETIRED_DELEGATED_STATES`,
  `_RETIRED_DELEGATION_REASON`, and the retired branch of `_effective_state`, which
  duplicates the model logic for raw records. `telemetry rebuild` then skips those records
  as unreadable, which the summary report already counts.
- Tests: the four `test_models.py` cases at lines 352 to 378, plus the delegation cases in
  `tests/test_store.py` and `tests/test_telemetry.py`.

### 5. Interrupted legacy updates

0.7.0 and 0.8.0 orphan a run with `pending-update.json` on the next `start`, so by 0.9.0
the only such runs are ones nobody has started since 0.6.0. Collapse the four sites into one.

- `agentic_preflight/store.py`: in `load_run`, treat a nonterminal record with a
  `pending-update.json` beside it as a `RunReadError` with reason `invalid` and a message
  naming 0.7.0 as the release that can finish or abort it.
- Delete `_interrupted_legacy_update` and its callers in `runs/_session.py`
  (`_load_current`), `runs/start.py` (the orphan-and-restart branch around line 272), and
  `runs/lifecycle.py` (the `status` branch around line 612 and the
  `interrupted_legacy_update` envelope key).
- `runs/evidence.py`: keep the single `orphaned_reason == "interrupted legacy update"`
  comparison through 0.9.0. Records that 0.7.0 orphaned persist that string, and the
  comparison is what keeps their green evidence out of reuse. Drop it in 0.10.0 together
  with the string constant. Replace the `pending-update.json` existence check there now,
  since `load_run` rejects those records first.
- Docs: `skill/reference/commands.md` lines 83 to 90, the row in `skill/SKILL.md` line 200,
  `docs/state-machine.md` line 158. `tests/test_docs_match_cli.py` pins the state-machine
  doc, so update the doc and the test fixture together.
- Tests: `tests/test_restart_limit.py`, `tests/test_cli_m0.py`,
  `tests/test_evidence_refresh.py`, `tests/test_store.py`.

### 6. Config keys removed in 0.6.0

`("review", "require_fix_commits")` and `("worktree", "ttl_hours")` were removed in 0.6.0,
so only 0.5.x snapshots contain them. Remove the two entries from `REMOVED_CONFIG_KEYS`
in `agentic_preflight/config.py` and their cases in `tests/test_config.py`. A 0.5.x note
then fails verification with `incompatible_schema`-style recovery text, which is correct:
0.6.0 already refused to read 0.5.x run records.

## Stage 2: 0.10.0

### 7. Null retired attestation fields

Attestation notes outlive runs: a branch verified under 0.6.0 can still be open when the
verifier moves to 0.9.0. Keeping `discard_retired_null_fields` one release longer costs
six lines and avoids forcing a re-run on an open PR. Delete it, and the matching cases in
`tests/test_models.py`, `tests/test_store.py`, `tests/test_evidence_transport.py`, and
`tests/test_telemetry.py`, in 0.10.0.

### 8. Config sections removed in 0.7.0

`("reuse", "*")`, `("ci", "*")`, and `("hook", "enabled")` serve two readers: historical
snapshots in notes, and user configuration files that still carry the sections. In 0.10.0
both become plain unknown-key errors. Delete the entries, `discard_retired_sections`, the
wildcard warning branch around `config.py` line 339, and `snapshot_config`'s filtering.
At that point `REMOVED_CONFIG_KEYS` is empty and should be deleted rather than left as an
invitation.

### 9. Old sibling worktree directory

`legacy_worktrees` only reports, but it also feeds runner reclamation in
`runs/lifecycle.py` around line 361 and appears in `gc` envelopes, `skill/SKILL.md`,
`skill/reference/commands.md`, and `docs/worktree-modes.md`. Two releases after the
default moved is long enough for `gc` to have reported them. Delete `LEGACY_DIRNAME`,
`legacy_worktrees`, the `data.legacy_worktrees` key, and the sibling-path candidates in
`_reclaim_released_runners`. Keep the commented `root` example in `initcmd.py` since an
explicit root of that name stays valid. Note the envelope key removal in
`COMPATIBILITY.md`, since `gc` consumers may read it.

### 10. Producer key becomes required, but stays nullable

`producer` itself is not a shim and never becomes required. A 0.7.0 record has no
producer, and 0.8.0 rewrites it with `"producer": null` rather than inventing one, so
0.10.0 still meets null producers inside its support window and must read them. Null is
the honest value for "unknown", and the summary keeps reporting it as such.

What does expire is the missing key. Every record 0.8.0 writes carries the key, so in
0.10.0 the `record.get("producer")` fallback in `telemetry._producer`, which exists for
raw records without it, can go. The model field stays `Producer | None`.

## Guardrail: keep this from growing back

Add `agentic_preflight/_compat.py` holding one table:

```python
REMOVALS: Final = (
    # (identifier, introduced_in, remove_in)
    ("models.MergebackAttempt.discard_legacy_rebased_tree", "0.7.0", "0.9.0"),
    ...,
)
```

and one test that parses `pyproject.toml`'s version and fails when any row's `remove_in`
is less than or equal to it. Each shim's docstring names its row. A shim without a row is
a review finding. `AGENTS.md` gets one sentence under "Docs that change with code": a
compatibility shim adds a `REMOVALS` row with its removal release.

Two further habits remove most future rows before they are written:

- A change that lands between tags needs no shim. Items 1 and 2 would not have existed.
- Prefer rejection with recovery text over rewriting state on load. Item 4 cost two
  implementations (model and telemetry) because the rewritten state then had to be
  recognised downstream.

`COMPATIBILITY.md` becomes a policy page: supported platforms, the previous-minor rule,
and a pointer to the changelog. Per-release detail moves to `docs/upgrade-<version>.md`.
