# ADR 004: Local Publisher reads with a distinct sensitive-data gate

Status: accepted for implementation under issue #19.

## Context

The next GET batch spans product catalogs, artifact/configuration metadata and
records containing purchaser identities, tokens, addresses and financial details.
Current hosted consent does not describe that expanded surface. Generic read scope
must not silently add sensitive methods to existing hosted grants.

## Decision

Implement 19 ordinary and eight sensitive fixed GET operations with separate
whitelists/tool names. Both tools are local-only in this increment. Sensitive
methods require `GOOGLE_PLAY_SENSITIVE_READ_PACKAGES` within read app scope,
independently from all mutations. Do not extend the existing edit-reader whitelist
with tester groups, which can identify people/organizations.

Validate pinned types plus documented prose constraints before credentials. Extract
one exact package from each route, including external transaction resource names.
Encode identifiers; retain repeated query values and exact integer/string formats.
Return one object/page and preserve data/pagination without following provider URLs.

Authorized sensitive responses remain intact. Wrapper metadata, errors and logs
must not echo sensitive input IDs or URLs. Disclose that the selected MCP client
receives provider PII/tokens; redaction of transport errors is not anonymization.

## Deferred boundaries

Account user/grant methods need explicit developer-account scope. Alternative
app-store catalog methods require dual app-store/target-app authority. Media
streams need bounded binary transport and artifact-output handling. Hosted
expansion requires versioned consent and account-isolation acceptance, rather than
reusing old grants for new personal/financial data.

[Method scope, examples and primary references](../publisher-reads.md).
