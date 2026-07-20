# Docs rubric

## The obligation test

One question governs this entire stage:

> **Would a reader following the current documentation now be wrong?**

Not "could the docs be better." Not "is this documented as well as it could be."
Documentation can always be improved, and a stage that reports every possible
improvement generates noise on every run — and a noisy gate is a gate someone
disables. This rubric is built around obligation, not aspiration.

**Zero findings is a normal, common, and correct outcome.** Most code changes create no
documentation obligation at all. Reporting zero is doing the job properly.

## When the diff creates an obligation

Ask whether the change makes an existing statement false, or leaves a documented
surface incomplete in a way a reader would trip on.

| Diff contains | Obligation | Typical action |
|---|---|---|
| New CLI flag or command | README/usage lists flags but not this one | `auto_fix` |
| Changed public API signature | A documented example no longer runs | `auto_fix` |
| Renamed config key | Docs name the old key | `auto_fix` |
| Changed default value | Docs state the old default | `auto_fix` |
| Removed feature | Docs still describe it as available | `auto_fix` |
| Changed behaviour under a documented contract | Documented guarantee is now ambiguous or wrong | `ask_user` |
| New env var required to run | Setup instructions would leave a reader stuck | `auto_fix` |
| Internal refactor, no surface change | **None** | no finding |
| New private helper | **None** | no finding |
| Test-only change | **None** | no finding |
| Performance improvement, same behaviour | **None** | no finding |

## Severity

- **`high`** — following the docs now produces a wrong result or a failure. A
  documented example that errors; a documented default that is no longer the default.
  Blocks.
- **`medium`** — a real gap that will confuse, but does not make anyone wrong. A new
  flag absent from an otherwise exhaustive list. Does not block by default.
- **`low`** — a nit. Prefer no finding at all.

Docs findings below `high` do not block, deliberately. The stage should improve
documentation without becoming a reason to turn the gate off.

## Where a finding may land

Docs findings may target files the diff never touched — that is the whole reason the
stage exists. The code changed; the doc that should have changed did not.

But the target must be documentation. `context --section docs` returns `doc_surface`,
the code-built inventory of this repo's documentation:

- `README*`, `CONTRIBUTING*`, `CHANGELOG*`
- `CLAUDE.md`, `AGENTS.md`
- `docs/**`
- anything in `[docs] paths`

A finding against a source file is rejected. If you found a code problem during the
docs stage, that is a review finding you missed — note it to the user rather than
smuggling it through here.

## The changelog

If `[docs] require_changelog` is enabled and the diff does not touch the changelog, the
CLI injects that finding itself. You do not need to check for it, and you should not
submit a duplicate. Mechanical rules belong to code precisely because they are the ones
a reviewer forgets on the twentieth run.

## Writing a good docs finding

Bad:

> "The README could use more detail about the new flag."

Good:

> **title:** `README quick-start omits the required --config flag`
> **detail:** `The quick-start block shows 'app run', but as of this change 'run'
> exits 2 without --config. A reader copying that command gets an error.`
> **suggestion:** `app run --config ./app.toml`

Name the file, name what is now false, and show the replacement.
