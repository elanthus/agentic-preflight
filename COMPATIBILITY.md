# Compatibility policy

Agentic Preflight supports Python 3.11, 3.12, and 3.13 on macOS 15 or newer, on Linux,
and on Windows 10 or newer. Git 2.38 or newer is required.

Windows support is native: it does not go through WSL, and it does not require a POSIX
shell for ordinary use. Two Windows-specific notes are worth knowing before adopting it:

- **A stage command containing shell grammar needs Git Bash.** Commands are executed
  directly as a program and its arguments wherever possible, so `pytest`,
  `ruff check .`, and `npm run test` need no shell at all. A command using pipes,
  `&&`, redirection, or globs falls back to a shell, and on Windows that shell is the
  one Git for Windows installs. It is found through the Git installation rather than
  through `PATH`, because `bash.exe` on `PATH` is normally the WSL launcher, which
  would run the command against a different filesystem.
- **Symlink-related behaviour requires Developer Mode.** Creating symlinks is a
  privileged operation on Windows by default. This affects repositories that contain
  symlinks; nothing else in the tool creates one.

## Stage commands and your shell profile

This is not Windows-specific, and it is the one behavioural difference worth
understanding on every platform.

A stage command is executed directly as a program and its arguments whenever it has
no shell grammar in it. A directly executed command does not source your login shell
profile. That matters when a version manager — `nvm`, `pyenv`, `rbenv`, `mise`,
`asdf` — puts its shims on `PATH` from that profile rather than from your
environment: a stage may then run the system build of a program instead of the one
the version manager would have selected, and nothing reports the difference.

A command whose program cannot be found without the profile is unaffected: resolution
fails, the command falls back to a shell, and the profile is sourced as before. The
gap is only for a program that exists in both places.

Select the interpreter in the command itself if a stage depends on one:

```toml
[commands]
test = "uv run pytest"
```

This is worth doing regardless. A hosted CI job does not source your profile either,
so a command that relies on it already behaves differently there than it does locally.

Program resolution is PATH-only: a bare command name is never looked up in a working
directory, so a repository cannot supply the program that validates it.

## Validation tiers

The supported combinations receive different validation frequencies so pull-request
feedback stays fast:

- Pull requests and pushes to `main` run on `ubuntu-latest` with Python 3.13
  to conserve GitHub Actions minutes.
- A scheduled regression run covers the oldest supported boundary, macOS 15 with
  Python 3.11, every Monday and Thursday.
- Manual CI runs and release tags cover Python 3.11, 3.12, and 3.13 on
  `ubuntu-latest`, `macos-latest`, and `windows-latest`.

A platform is supported even when it is not in the pull-request job. A failure that is
specific to a supported combination is a release blocker and should be fixed with the
same priority as a pull-request CI failure.

Other POSIX systems and newer Python prereleases may work, but they are best effort
until they are added to the supported matrix. Successful installation on a version
outside the matrix does not make that version supported.

## Compatibility changes

The release after 0.6.0 requires Click 8.2 or newer. Documentation inventory entries
no longer contain `exists`, and detected command candidates no longer contain `trust`;
consumers should use `path`, `size`, `touched_by_diff`, and candidate `source`. Workflow
sources retain their `untrusted:workflow:` prefix. Attestation fingerprints are unchanged.

The command-line interface, configuration file, and attestation schema follow Semantic
Versioning. The Python modules are internal and have no compatibility promise. While
the version is 0.x, a minor release may include breaking changes, and each one is
called out in `CHANGELOG.md`.

Exit code 6 is new in the release after 0.6.0 and means a command-line usage error.
Earlier releases exited 2 for usage errors, the same code as a failed stage, and
printed nothing on stdout. A wrapper that treated exit 2 from a malformed invocation
as a stage failure should now expect 6 and a `usage_error` envelope. `hook-check`
still exits 2 on a usage error.

Agentic Preflight 0.6.0 does not read run records or attestation notes written by
earlier releases. Unsupported saved data is reported as an error and left unchanged with
the work and ownership information it refers to.

Attestation verification ignores configuration keys that a later release removed, such
as `[hook] enabled`, when they appear in an attested `config_snapshot`, so a newer
verifier accepts notes written by an older release; a configuration file that sets one
is still rejected.

## Owner review approval

`[approval] mode = "owner_review"` and its required `reviewer` GitHub login are new
opt-in configuration. Existing `manual_merge`, `environment`, and `peer_review` behavior
and attestation schemas are unchanged. Older versions cannot load the new mode; upgrade
the trusted protected-base consumer before enabling it. Policy changes are evaluated
under the old protected-base mode until merged.

The new `reviewer` field defaults to an empty string in configuration snapshots; old
snapshots remain readable by this version. Snapshots written by this version include
that new field and may be rejected by older strict configuration readers.

Isolated worktrees now default to the user cache (`$XDG_CACHE_HOME` when absolute,
otherwise `~/.cache`) instead of the sibling `.agentic-preflight-worktrees` directory.
Existing configured roots remain supported. New runs use the new default; `gc` may
reclaim eligible unused reusable runners at either location, preserving dirty,
locked, leased, or retained work and reporting remaining legacy registrations.

## Saved merge-back attempts

Saved schema-v2 runs may contain the former `mergeback_attempt.rebased_tree`
field. It is accepted and ignored on load, and omitted on the next write. Other
unknown attempt fields remain invalid. A pending source rebase that has not reached
the recorded verified tree now becomes stale; restart preflight to validate the
current source rather than resuming from that intermediate tree.
