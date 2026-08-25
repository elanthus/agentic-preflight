# Compatibility policy

Agentic Preflight supports Python 3.11, 3.12, and 3.13 on macOS 15 or newer, on Linux,
and on Windows 10 or newer. Git 2.30 or newer is required.

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

- Pull requests and pushes to `main` run on `ubuntu-latest` and `windows-latest` with
  Python 3.13. Windows is in the pull-request job rather than a scheduled one because
  it is the platform whose failures are least likely to be noticed by a contributor
  working on macOS or Linux.
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

The command-line interface, configuration file, attestation schema, and documented
Python API follow Semantic Versioning. During the current `0.x` series, incompatible
changes may ship in a minor release and will be called out in `CHANGELOG.md`.

Removing a Python or operating-system version should be announced in the changelog at
least one minor release in advance when practical. An upstream end-of-life or hosted
runner retirement may require a faster change; in that case the release notes must
identify the constraint and the last compatible Agentic Preflight release.
