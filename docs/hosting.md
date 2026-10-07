# Self-hosting for the operator's own Google accounts

PubShip is local-first. The project does not run a Google-connected service. Optional HTTP mode is for infrastructure you control and your own Google accounts, not public customer enrollment.

## Required configuration

Install `pubship[hosted]`, then configure a dedicated Google OAuth web client whose redirect URI is your chosen HTTPS issuer plus `/google/callback`. Store the client JSON and vault key as private files outside the repository. Their parent directories must be private; the runtime must own its writable vault and transfer directories. Never reuse local ADC or gcloud credentials in HTTP mode.

| Variable | Purpose |
| --- | --- |
| `PUBSHIP_SERVER_ALLOWED_EMAILS` | Mandatory comma-separated exact Google account emails belonging to the operator. No wildcards, aliases or domain grants. Match the spelling Google returns. |
| `PUBSHIP_SERVER_ISSUER` | Your HTTPS origin, without path or query. |
| `PUBSHIP_SERVER_GOOGLE_CLIENT_FILE` | Absolute path to the private Google web-client JSON. |
| `PUBSHIP_SERVER_VAULT_KEY_FILE` | Absolute path to a private Fernet encryption key file. |
| `PUBSHIP_SERVER_VAULT_DATABASE` | Absolute path to the encrypted SQLite vault. |
| `PUBSHIP_SERVER_TRANSFER_DIRECTORY` | Absolute path to private transfer storage. |
| `PUBSHIP_SERVER_NEW_ENROLLMENT_ENABLED` | Defaults to `false`. Explicit `true` allows new connections only for allowlisted operator accounts. |

The prefix change preserves each previous setting's suffix. Local `GOOGLE_PLAY_*` settings retain their names and never grant HTTP connection authority. Capacity and transfer settings are documented in [vault recovery](vault-recovery.md) and [transfers](hosted-transfers.md).

```sh
uv sync --frozen --extra hosted
uv run --frozen --extra hosted pubship-server --host 127.0.0.1 --port 8080
```

## Identity and permissions

Sign-in requests OpenID identity and selected Google API scopes. The server validates the Google signature, issuer, OAuth client audience, expiry, nonce and `email_verified=true`. The exact verified email must be allowlisted before Google tokens or a connection grant are stored. Browser-provided email values cannot substitute for verification. Temporary browser consent state contains no Google credentials.

Stored grants must contain verified identity and still match the current allowlist when tokens are issued or used. Older grants require fresh sign-in. Removing an email and restarting with the changed configuration blocks that account's existing grants. Revoke connections explicitly when retiring access. Enrollment closure blocks new connections while existing allowed connections retain their prior scope.

Separate [capability consent](hosted-capabilities.md) binds exact apps, methods and targets to each connection. Google permissions are an additional ceiling. There is no credential fallback, anonymous data access or voided-purchases execution path.

## Runtime boundaries

Use one process and one replica with a TLS reverse proxy. The image runs as UID/GID 10001 and contains no private configuration. Use private persistent vault storage and a separate private transfer directory. The operator manages keys, permissions, storage budgets, backups and updates. Multi-replica operation is unsupported.

The proxy must preserve the configured Host and support Streamable HTTP. `/healthz` is liveness only. `/mcp` requires OAuth. Use the exact issuer host in health probes. Disable request URL/body and token logging. Do not expose the internal port around the HTTPS proxy. Review timeout and upload limits against the [transfer contract](hosted-transfers.md).

Use [reconnect-only vault recovery](vault-recovery.md). Do not restore historical credentials into an active vault. Test synthetic configuration and rejection paths before using your own accounts; successful fake tests do not verify a provider entitlement or live mutation.
