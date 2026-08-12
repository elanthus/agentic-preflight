# Security policy

## Supported versions

Security fixes are applied to the latest published release and the `main` branch. Older
releases are not maintained with backported security patches. Runtime and operating
system support is defined in [COMPATIBILITY.md](COMPATIBILITY.md).

## Automated security controls

Pull requests and `main` are checked with CodeQL and an audit of the locked runtime
dependency set. Scheduled scans catch newly published advisories even when the source
tree has not changed. Dependabot proposes updates for Python and GitHub Actions
dependencies.

Release distributions include a CycloneDX SBOM and receive GitHub artifact attestations
for build provenance and the SBOM. These preventive controls improve traceability and
detection; their presence is not evidence of a current known vulnerability, and an
attestation does not establish that an artifact is vulnerability-free.

## Reporting a vulnerability

Please do not disclose a suspected vulnerability in a public issue, discussion, or pull
request. Use [GitHub private vulnerability reporting](https://github.com/elanthus/agentic-preflight/security/advisories/new)
or email [tokenmagic33@gmail.com](mailto:tokenmagic33@gmail.com).

Include the affected version or commit, reproduction steps, the likely impact, and any
suggested mitigation. You should receive an acknowledgement within seven days. After
validation, the maintainer will coordinate remediation and disclosure with you.

This project is an advisory quality gate rather than a security boundary. Reports that
demonstrate a bypass beyond the documented `--no-verify`, missing-tool fail-open, and
manual-mode limitations are especially useful.
