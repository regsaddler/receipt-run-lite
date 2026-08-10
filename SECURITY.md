# Security policy

## Reporting a vulnerability

Please use GitHub's **Security** tab and the repository's private vulnerability
reporting form. Do not open a public issue for an unpatched vulnerability, and
do not include real credentials, private paths, or production data in a report.

Include the affected revision, operating system, a minimal reproduction, the
expected containment boundary, and the observed result. Reports that distinguish
an integrity failure from a documented non-goal are especially useful.

## Supported version

Security fixes target the latest revision on `main`. This repository is a small
reference utility and does not maintain parallel supported release branches.

## Security boundary

`receipt-run-lite` records local command observations. It is not a sandbox,
privilege boundary, signed attestation system, or independent validator. Run
only commands you already trust at the privilege level of the invoking user.
