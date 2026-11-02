# Security policy

## Supported versions

Security fixes apply to the latest published release and to the `main` branch.
Older releases do not receive backported security patches.
[COMPATIBILITY.md](COMPATIBILITY.md) defines the supported runtimes and operating systems.

## Automated security controls

CodeQL scans pull requests and `main`.
An audit of the locked runtime dependencies also runs on each of them.
Scheduled scans find newly published advisories when the source tree has not changed.
Dependabot proposes updates for Python and GitHub Actions dependencies.

Each release distribution includes a CycloneDX SBOM.
Each release also receives GitHub artifact attestations for build provenance and for the SBOM.
These controls improve traceability and detection.
Their presence is not evidence of a current known vulnerability.
An attestation does not establish that an artifact is free of vulnerabilities.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue, discussion, or pull request.
Use [GitHub private vulnerability reporting](https://github.com/elanthus/agentic-preflight/security/advisories/new)
or send an email to [tokenmagic33@gmail.com](mailto:tokenmagic33@gmail.com).

Include the affected version or commit, the steps to reproduce, the probable impact, and a suggested mitigation if you have one.
You will receive an acknowledgement within seven days.
After validation, the maintainer coordinates remediation and disclosure with you.

Agentic Preflight is an advisory pre-push check. It is not a security boundary.
The documented limits are the `--no-verify` bypass, the fail-open behavior when the tool is missing or its configuration cannot be loaded, manual mode, and unsigned attestations.
Attestations are unsigned Git notes. Anyone with permission to update `refs/notes/agentic-preflight` can write a note that claims stages passed.
`verify` takes its merge base and its policy from the protected base, not from the note, so such a note must still satisfy the committed base policy.
See [the limits guide](docs/limits.md#attestations-are-not-signatures).
A report that demonstrates a bypass beyond these limits is especially useful.
