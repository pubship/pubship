# Claude Code instructions

Read and follow [AGENTS.md](AGENTS.md).

# PubShip project instructions

Owner: Denys Vorobyov. AGPL-3.0-only is fixed. No outside code contributions. Accept bug reports, ideas and questions as issues, not submitted code. Preserve third-party notices. No Google marks in product names or logos.

## Product boundaries

Local-first, with the developer's own credentials. No project-run Google service. Self-hosting is only for the operator's own Google accounts. PUBSHIP_SERVER_ALLOWED_EMAILS is mandatory and must match Google's cryptographically verified email before credentials are stored. Enrollment is closed by default. Never add ADC fallback to HTTP mode or bypass the allowlist. Voided-purchase data must never reach an MCP client.

Keep GOOGLE_PLAY_* environment names stable. HTTP configuration uses PUBSHIP_SERVER_*. Never commit credentials, account exports or operator configuration. Tests use fakes, with GOOGLE_APPLICATION_CREDENTIALS unset at invocation. Keep dependencies, CI actions and generated discovery metadata reproducible. Do not describe synthetic tests as live Google verification.

## Four-hat delivery

1. Architecture: review contracts, authorization, identity, storage and failure paths.
2. Engineering: implement focused changes and perform adversarial code review.
3. QA: test success, rejection, isolation, packaging and client behavior. Never weaken a failing check.
4. Delivery/TPM: keep scope, issue/PR links, README, release notes and validation evidence synchronized.

Keep issue -> branch -> PR -> checks -> owner review -> merge -> bundled release traceable. Never approve a review or GitHub deployment yourself. Do not rewrite published tags or assets. Never add unsolicited commit attribution.

## Documentation and website

Review https://github.com/pubship/website in the same delivery cycle for every feature, authentication, release or data-handling change. Record exact claims checked when no website change is needed. Distinguish available source, published packages and deployments. Keep README, Claude Code, Codex and generic MCP client instructions aligned. No em-dashes in shipped files.
