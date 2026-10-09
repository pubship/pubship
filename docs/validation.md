# Validation

Repository acceptance uses synthetic providers only. Historical deployment receipts are excluded and do not establish acceptance of a new candidate. These checks do not claim live Google reads or mutations.

Required checks: complete pytest suite, ruff check and format check, all generator checks, wheel and source builds, artifact inspection, secret scan, website checks and browser tests. Report exact command output, test counts, skips and failures in the release acceptance report. A check has not passed until it has actually run on the candidate.

The [provider verification index](provider-verification.md) links related suites, not live provider execution. Client and platform coverage must be reported separately. The operator allowlist has dedicated success, rejection, startup and legacy-grant tests.

The [tool definition quality gate](tool-definition-quality.md) checks actual local and self-host MCP surfaces with pinned TDQS offline lint and contract snapshots. It complements execution tests; it is not a model-scored grade or evidence that a directory has refreshed its cached definitions.
