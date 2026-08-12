# Compatibility policy

Agentic Preflight supports Python 3.11, 3.12, and 3.13 on macOS 15 or newer and on
Linux. Windows is not supported because the implementation requires Bash, `fcntl`, and
other POSIX behavior. Git 2.30 or newer is required.

## Validation tiers

The supported combinations receive different validation frequencies so pull-request
feedback stays fast:

- Pull requests and pushes to `main` run on `ubuntu-latest` with Python 3.13.
- A scheduled regression run covers the oldest supported boundary, macOS 15 with
  Python 3.11, every Monday and Thursday.
- Manual CI runs and release tags cover Python 3.11, 3.12, and 3.13 on both
  `ubuntu-latest` and `macos-latest`.

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
