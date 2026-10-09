# Changelog

## Unreleased

### Changed

- Explain tool parameters, result semantics and mode-specific prerequisites in actual MCP definitions.
- Correct prepare-operation and self-host download annotations to reflect their temporary state changes. Execution permissions and confirmation gates are unchanged; clients may adjust approval prompts.

### Fixed

- Keep the synthetic edit expiry stable across stdio prepare/apply calls so clock boundaries do not cause false stale-edit failures. Production stale-edit checks are unchanged.

### Added

- Pinned offline TDQS 1.2 lint for all three tool surfaces, with explicit review of structural heuristic candidates.
- Contract regression checks for tool ordering, schemas, defaults, annotations and caller overrides.

## 0.24.1

### Added

- Cursor plugin and Gemini CLI extension manifests for local setup with your own credentials.
- `llms-install.md` for agent-assisted installation and a 400 by 400 pixel PubShip logo.

### Changed

- PyPI Homepage points to https://pubship.dev; Repository and Issues link to GitHub.

## 0.24.0

- Distribution, import package and MCP name are `pubship`. Commands are `pubship` and `pubship-server`.
- Local use with your own Google credentials is the primary experience. The project does not operate a Google-connected service.
- Self-host configuration uses `PUBSHIP_SERVER_*`; local `GOOGLE_PLAY_*` names remain unchanged.
- Self-host startup requires an exact operator email allowlist. Signed Google identity, verified email and nonce are checked before storing credentials. Old or removed-account grants cannot be reused.
- Voided-purchases access is disabled in all execution paths. The inventory preserves its official definition as policy disabled.
- Legal notices, client setup, website and distribution workflows use the new identity. Outside code contributions are not accepted.

## Validation

This candidate must pass the full synthetic suite, lint/format, generator checks, packaging, artifact inspection, secret scanning and website checks before publication. No real Google credentials or production mutations are used for migration acceptance. Publication and deployment require separate owner gates.
