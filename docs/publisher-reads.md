# Publisher catalog and sensitive reads

These tools implement 27 fixed GET operations. Local stdio setup is described
below. Published and controlled-deployed 0.19.0 permits the 19 ordinary reads with explicit
`play:publisher-metadata` consent and the eight
sensitive reads with separate customer-data and tester-audience consent. Existing
connections gain no authority automatically. See [client setup](clients.md) for
Codex, Claude Code and a generic harness; the [hosted capability guide](hosted-capabilities.md)
defines connection and app boundaries. Google permissions still apply per endpoint.

## Ordinary catalog/configuration data

`read_publisher(method, parameters)` supports 19 operations for apps in
`GOOGLE_PLAY_PACKAGES`:

- Device tier configuration get/list and recovery action list.
- Existing-edit expansion-file metadata.
- Generated APK metadata list and system variant metadata get/list.
- Legacy in-app product batchGet/get/list.
- One-time product batchGet/get/list and purchase-option offer list.
- Subscription batchGet/get/list and base-plan offer get/list.

Use `list_api_methods` and `describe_api_method` for exact method names and pinned
contracts. Example:

```json
{
  "method": "androidpublisher.monetization.subscriptions.list",
  "parameters": {"packageName": "com.example.app", "pageSize": 50}
}
```

Each call returns at most one provider page. A real empty HTTP 204 is represented
as `data: null`, `content_present: false`, `http_status: 204`. It does not establish
a zero record count or resource absence. HTTP 200 with empty/invalid JSON remains
an error; JSON null is not silently treated as HTTP 204. Copy the actual next-page token into a new
explicit call when needed. Missing fields/empty arrays remain as Google returned
them; missing rows do not mean zero. Artifact URLs are data, not downloaded files.
No method creates an edit, uploads, changes products or follows arbitrary URLs.

## Sensitive reads require another opt-in

Add to your local client environment and restart:

```text
GOOGLE_PLAY_PACKAGES=com.example.app
GOOGLE_PLAY_SENSITIVE_READ_PACKAGES=com.example.app
```

This exposes `read_sensitive_publisher(method, parameters)` for eight operations:
order get/batchget; legacy product-purchase get; product-v2 purchase get;
subscription-v2 purchase get; voided-purchase list; external-transaction get; and
existing-edit tester configuration get. It is independent of all write permissions.

For hosted 0.19.0 connections, the seven customer reads need
`play:customer-data` and the tester read needs `play:tester-audience`, each for
explicitly selected apps. These return raw authorized fields to the MCP client,
including personal information and tokens; purchase prepare/apply redaction does
not apply to this read tool. A financial-action grant alone does not authorize it.
The local environment settings above never grant hosted access.

Provider records can contain account identities, subscriber names/email, buyer
addresses, purchase/linked tokens, revenue and Google Group addresses. The selected
MCP client receives the actual authorized response; the tool does not silently
remove fields and claim the remainder is complete. Consider the client's storage,
transcripts and access when choosing to enable these tools. Use synthetic values in
shared examples, issues and support logs.

The tool does not echo sensitive request parameters, opaque IDs or request URLs in
wrapper metadata or errors. Provider failures are redacted. This does not remove
sensitive values from the returned provider data itself.

## Contract and scope behavior

The method must be on the selected tool's whitelist. Requests are checked against
pinned contracts, additional documented constraints and exact app scope before
credentials or network access. External transaction resource names must have the
form `applications/{package}/externalTransactions/{id}` with an allowed package.
There is no arbitrary host, URL or parent-resource input.

Integer fields and int64-string fields keep their documented types. Repeated query
IDs are sent as repeated query parameters, not comma-joined or serialized JSON.
Boolean query values use lowercase true/false. Product/order batch methods require
explicit bounded nonempty ID lists; the legacy SKU batch has a local limit of 100
IDs, not a claimed Google limit. Recovery reads require the documented version.
When offer listing uses productId `-`, its base-plan or purchase-option scope must
also use `-`. These wildcards never widen the allowed app scope.

Deprecated pagination fields and provider rules remain visible. In particular,
legacy product maxResults/startIndex do not guarantee limiting provider pages;
voided-purchase access is disabled entirely, including pagination.
No automatic pagination, retries, financial interpretation or provider-link fetch
is performed. Existing-edit metadata is marked as an edit snapshot, not live state.

### Deliberately bounded local inputs

These are local supported-input limits, not claims about every identifier Google
might issue. Path identifiers reject `/`, backslash, `%`, controls, lone Unicode
surrogates and complete `.` / `..` segments; pass raw identifiers, not pre-encoded
paths. Other printable punctuation is URL-encoded. Purchase path tokens are at
most 4096 characters, other opaque IDs 1024, package names 255 and track IDs 100.
Pagination tokens preserve printable Unicode/punctuation up to 4096 characters.

Legacy SKU batches are capped locally at 100 distinct IDs. `maxResults` is locally
bounded to 1–1000 even where the legacy provider ignores it. Device configuration
pageSize is 1–100; modern product/offer lists allow 1–1000. JSON integer fields
reject booleans/floats rather than coercing them. Timestamp strings are nonnegative
int64 values; Google enforces its current-time and available-history constraints.

## Verification limits

Every listed route and boundary requires synthetic contract and adversarial tests,
including real MCP schema/error behavior. Safe live catalog reads may be verified
with an explicitly allowed app; purchase/customer exports are not required for QA.
See [verification](validation.md) and the release PR for actual test receipts.
Successful synthetic tests do not establish a live purchase integration or policy
approval. The candidate hosted expansion has separate consent; self-host configuration and provider permissions remain independent checks.

Primary references: [Publisher API](https://developers.google.com/android-publisher/api-ref/rest),
[subscription batches](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.subscriptions/batchGet),
[order batches](https://developers.google.com/android-publisher/api-ref/rest/v3/orders/batchget),
[voided purchases](https://developers.google.com/android-publisher/api-ref/rest/v3/purchases.voidedpurchases/list).

HTTP no-content semantics: [RFC 9110 §15.3.5](https://www.rfc-editor.org/rfc/rfc9110.html#name-204-no-content).
