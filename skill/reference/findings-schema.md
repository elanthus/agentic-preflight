# Findings schema

## What you send

```json
{"findings": [
  {
    "path": "src/auth.py",
    "line": 42,
    "severity": "critical",
    "action": "auto_fix",
    "title": "Password compared with ==",
    "detail": "Timing-variable comparison leaks information about the stored value.",
    "suggestion": "if secrets.compare_digest(supplied, stored):"
  }
]}
```

A bare JSON list is also accepted. An empty list is valid — and in the docs stage it is
the most common correct answer.

| Field | Required | Notes |
|---|---|---|
| `path` | yes | Repo-relative. Must resolve inside the worktree |
| `line` | no | 1-based. Must be within the file. Omit for file-level findings |
| `severity` | yes | `critical` \| `high` \| `medium` \| `low` |
| `action` | yes | `auto_fix` \| `ask_user` \| `no_op` |
| `title` | yes | One line, ≤ 200 chars. Say what is wrong, not what to do |
| `detail` | no | ≤ 4000 chars. Why it matters and what goes wrong |
| `suggestion` | no | ≤ 4000 chars. Concrete replacement code |

## What you must not send

**`id` and `stage` are not yours to set.** They are assigned by the CLI, and sending
either is a hard validation error rather than a silently ignored field. This is
deliberate: a hallucinated ID that got quietly honoured would corrupt the
`respond --id` protocol for the rest of the run.

Any other unrecognised field is also rejected.

## What the CLI assigns

| Field | How it is determined |
|---|---|
| `id` | `F001`, `F002`, … append-only across the **whole run**. Docs findings continue review numbering; they do not restart |
| `stage` | Derived from the run's state at submission time |
| `status` | Starts `open`; becomes `fixed` / `dismissed` / `accepted` via `respond` |
| `fix_commit` | Set by `respond --commit`, after verification |

## Validation

Rejected with exit 3 (`invalid_findings`), all-or-nothing for the batch:

- `path` escaping the worktree — `../`, absolute paths, or symlinks pointing out
- `path` outside the changed-file set (review) or documentation allowlist (docs)
- `line` beyond the end of the file
- unknown `severity` or `action`
- fields over their length caps
- batch size over `[review] max_findings`

## What blocks

A finding blocks when it is **open** and either:

- `severity` is in `[review] blocking_severities` (default `critical`, `high`), or
- `action` is `ask_user` — **at any severity**

`ask_user` blocking regardless of severity is the point of that action: you have
declined to decide, so proceeding without a human would decide by default.

Docs findings use `[docs] blocking_severities`, also `critical` and `high` by default,
which keeps routine documentation nits non-blocking.

## Choosing an action

**`auto_fix`** — mechanical, locally verifiable, and you can do it correctly right now
without anyone's input. A missing null check, a wrong comparison operator, an unhandled
exception. Most findings.

**`ask_user`** — the fix requires knowing intent: is this API meant to be public? Is
this behaviour change deliberate? Is this trade-off acceptable? If you would be
guessing at product or design intent, this is the action.

**`no_op`** — worth recording so it is visible, not worth acting on. Use sparingly:
findings nobody acts on are what makes people switch a gate off.
