# Changelog

## 0.24.0

- Distribution, import package and MCP name are `pubship`. Commands are `pubship` and `pubship-server`.
- Local use with your own Google credentials is the primary experience. The project does not operate a Google-connected service.
- Self-host configuration uses `PUBSHIP_SERVER_*`; local `GOOGLE_PLAY_*` names remain unchanged.
- Self-host startup requires an exact operator email allowlist. Signed Google identity, verified email and nonce are checked before storing credentials. Old or removed-account grants cannot be reused.
- Voided-purchases access is disabled in all execution paths. The inventory preserves its official definition as policy disabled.
- Legal notices, client setup, website and distribution workflows use the new identity. Outside code contributions are not accepted.

## Validation

This candidate must pass the full synthetic suite, lint/format, generator checks, packaging, artifact inspection, secret scanning and website checks before publication. No real Google credentials or production mutations are used for migration acceptance. Publication and deployment require separate owner gates.
