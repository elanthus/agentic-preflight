# Findings schema

## What you send for review

```json
{"coverage": {"manifest": "<64-character digest>", "examined": "all"},
 "findings": [
  {
    "unit": "U0007",
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

The manifest comes from `context`. `examined: "all"` is one compact assertion over the
complete delivered manifest: code marks units referenced by findings as cited and every
remaining unit examined clean. When `path` and `line` identify exactly one unit, `unit`
may be omitted and is assigned mechanically. Name it for deleted-only, binary, or
ambiguous multi-hunk findings.

Docs submissions remain `{"findings": [...]}` or a bare JSON list. An empty docs list is
valid and is the most common correct answer. A findings-only review submission is invalid.

| Field | Required | Notes |
|---|---|---|
| `unit` | when ambiguous | Review-unit ID returned by `context`; not used for docs |
| `path` | yes | Repo-relative. Must resolve inside the worktree |
| `line` | no | 1-based. Must be within the file. Omit for file-level findings |
| `severity` | yes | `critical` \| `high` \| `medium` \| `low` |
| `action` | yes | `auto_fix` \| `ask_user` \| `no_op` |
| `title` | yes | One line, ≤ 200 chars. Say what is wrong, not what to do |
| `detail` | no | ≤ 4000 chars. Why it matters and what goes wrong |
| `suggestion` | no | ≤ 4000 chars. Concrete replacement code |

## What you must not send

**`id`, `stage`, and `code_owned` are not yours to set.** They are assigned by the
CLI, and sending any of them is a hard validation error rather than a silently ignored
field. This is deliberate: a hallucinated ID that got quietly honoured would corrupt
the `respond --id` protocol for the rest of the run, while spoofed ownership would
bypass the repository's severity policy.

Any other unrecognised field is also rejected.

## What the CLI assigns

| Field | How it is determined |
|---|---|
| `id` | `F001`, `F002`, … append-only across the **whole run**. Docs findings continue review numbering; they do not restart |
| `stage` | Derived from the run's state at submission time |
| `code_owned` | `true` only for a mechanical requirement derived by the CLI; otherwise `false` |
| `status` | Starts `open`; becomes `fixed` / `dismissed` / `accepted` via `respond` |
| `fix_commit` | Set by `respond --commit`, after verification |

## Validation

Rejected with exit 3 (`invalid_findings`), all-or-nothing for the batch:

- review coverage missing or not matching the current diff snapshot
- an unknown review unit, or a unit belonging to another path
- `path` escaping the worktree — `../`, absolute paths, or symlinks pointing out
- `path` outside the changed-file set (review) or documentation allowlist (docs)
- `line` beyond the end of the file
- unknown `severity` or `action`
- fields over their length caps
- batch size over `[review] max_findings`

## What blocks

A finding blocks when it is **open** and either:

- `severity` is in `[review] blocking_severities` (default `critical`, `high`), or
- `action` is `ask_user` — **at any severity**, or
- `code_owned` is `true` — **at any severity**

`ask_user` blocking regardless of severity is the point of that action: you have
declined to decide, so proceeding without a human would decide by default.
Code-owned findings block regardless of severity because they record mechanical
requirements established by the CLI, not reviewer judgment.

Docs findings use `[docs] blocking_severities`, also `critical` and `high` by default,
which keeps routine documentation nits non-blocking without downgrading code-owned
requirements such as a mandatory changelog update.

A non-blocking finding may still be dispositioned while its review or docs stage is
green. Use `fixed --commit <sha>` to register a repair, or `accepted --note <reason>`
to preserve why a valid finding is not worth fixing. Repair commits change the reviewed
snapshot, so they return the run to review; note-only dispositions keep the stage green.

## Choosing an action

**`auto_fix`** — mechanical, locally verifiable, and you can do it correctly right now
without anyone's input. A missing null check, a wrong comparison operator, an unhandled
exception. Most findings.

**`ask_user`** — the fix requires knowing intent: is this API meant to be public? Is
this behaviour change deliberate? Is this trade-off acceptable? If you would be
guessing at product or design intent, this is the action.

**`no_op`** — worth recording so it is visible, not worth acting on. Use sparingly:
findings nobody acts on are what makes people switch a gate off.
