# ADR 001: Local-first; self-hosting only for the operator's own Google accounts; no project-run Google service

Status: accepted for 0.24.0.

PubShip runs locally with credentials controlled by the developer. Optional HTTP mode runs only on infrastructure controlled by its operator, for that operator's own Google accounts. The project runs no Google-connected service and never receives credentials.

HTTP startup requires PUBSHIP_SERVER_ALLOWED_EMAILS containing exact account emails. A signed Google ID token must verify issuer, audience, lifetime, nonce and email_verified=true, then match that allowlist before credentials or grants are persisted. Request form fields and client names cannot establish identity. Every later grant use checks the current allowlist. No local ADC fallback is permitted. Enrollment defaults to closed.

App, service and method grants still narrow each connection independently. The operator must maintain private storage, encryption keys, TLS, backups and upgrades. Voided-purchase data is excluded from MCP execution regardless of credentials or permissions. Provider definitions remain an inventory, not authority to invoke an endpoint.
