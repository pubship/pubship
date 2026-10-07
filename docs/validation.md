# Validation

PubShip 0.24.0 migration acceptance uses synthetic providers only. Historical deployment receipts are excluded from this repository and do not establish acceptance of this candidate. No live Google read or mutation is claimed for PubShip.

Required checks: complete pytest suite, ruff check and format check, all generator checks, wheel and source builds, artifact inspection, secret scan, website checks and browser tests. Report exact command output, test counts, skips and failures in the release acceptance report. A check has not passed until it has actually run on the candidate.

The [provider verification index](provider-verification.md) links related suites, not live provider execution. Client and platform coverage must be reported separately. The operator allowlist has dedicated success, rejection, startup and legacy-grant tests.
