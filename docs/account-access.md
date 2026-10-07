# Account user and grant administration

The seven Android Publisher `users` and `grants` methods are available through
three local tools: `read_account_access`, `prepare_account_access`, and
`apply_account_access`. Local stdio uses the operator's existing Publisher authentication and the explicit configuration below. The 0.21.0 package and controlled runtime separately register these tools with customer-owned credentials and exact administrative consent. Ordinary reads or publishing never grant account authority.

## Independent local scopes

Set these comma-separated environment variables, without wildcards:

| Variable | Exact values |
| --- | --- |
| `GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPERS` | Numeric Play developer account IDs |
| `GOOGLE_PLAY_ACCOUNT_ACCESS_METHODS` | Supported full method IDs below |
| `GOOGLE_PLAY_ACCOUNT_ACCESS_USERS` | Target user email addresses for mutations |
| `GOOGLE_PLAY_ACCOUNT_ACCESS_APPS` | App package names or numeric draft app IDs for grant changes and user deletion |
| `GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPER_PERMISSIONS` | Maximum developer permission enums the tool may change |
| `GOOGLE_PLAY_ACCOUNT_ACCESS_APP_PERMISSIONS` | Maximum app permission enums the tool may change |

Defaults are empty. Scopes form an explicit cross-product: each configured target
user/app can be managed in each configured developer account, subject to Google's
own authority checks. Run separate local processes if different accounts require
different scope combinations. A read grant permits the account's personal email
addresses, access states and permissions to be returned to the MCP client.
Mutations need the exact write method **and** `androidpublisher.users.list` so
preparation and apply can inspect all pages. Permission ceilings cover both the
existing permissions being modified and the requested permissions. User deletion
also requires every existing app grant and its permissions to fall within scope.
Deprecated and unspecified permission enums are not accepted as writable ceilings.

Supported methods:

- `androidpublisher.users.list`
- `androidpublisher.users.create`
- `androidpublisher.users.patch`
- `androidpublisher.users.delete`
- `androidpublisher.grants.create`
- `androidpublisher.grants.patch`
- `androidpublisher.grants.delete`

For example, a read-only local configuration is:

```sh
export GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPERS=123456789
export GOOGLE_PLAY_ACCOUNT_ACCESS_METHODS=androidpublisher.users.list
```

Use `describe_api_method` for provider field schemas. User resources are named
`developers/{developer}/users/{email}`; grant resources append
`/grants/{package_name_or_draft_app_id}`. Create bodies must supply the matching
resource name; user creates also supply the matching email. Output-only fields
are refused. Patch calls require an explicit `updateMask` drawn from
`developerAccountPermissions,expirationTime` for users or `appLevelPermissions`
for grants. Omitting a masked value explicitly resets that field; resetting
`expirationTime` removes the expiry. Supplied expirations must be future RFC 3339
timestamps. User emails and app identities cannot be changed through patch.

## Hosted execution: exact authority and privacy

Request `play:account-directory`, `play:account-users` or `play:account-grants` independently, then select Publisher access and the exact targets in browser consent. Directory access selects developer IDs and returns one account-wide raw page of personal emails, access state and permissions to the MCP client. Mutation consent permits the internal scan needed to inspect the target but does not grant this raw-directory tool.

User operations bind an exact developer/user pair, selected methods, developer-permission ceiling and affected-app permission ceilings. App-grant operations bind an exact developer/user/app triple, selected methods and that app's permission ceiling. These are exact rows, unlike the local cross-product above. Unlisted combinations, duplicate rows, wildcards and unsupported permission enums are rejected. Empty ceilings allow only empty permissions. Administrative developer permissions apply across the Google developer account; an app selection cannot restrict the recipient's later independent Console/API access.

User deletion must cover every existing app grant. Changing expiry additionally requires `allow_expiration_change` and coverage of every affected app and permission, even when shortening access. Omitting a masked `expirationTime` removes expiry, so review the indefinite-access effect. Selecting user creation separately includes the initial expiration choice. Account-only consent can have no ordinary app packages; it grants no ordinary app reads.

The native form offers three user rows, three grant rows and three affected-app rows per user. Backend validation permits 100 user/grant targets combined and 100 affected-app entries combined; that is not a promise that the browser form can enter 100 rows. Directory IDs are separately bounded at 100. A readable exact-authority review comes before Google sign-in. Version 3 grants preserve dimensions; legacy/v1/v2 grants remain unchanged through upgrade and refresh.

Hosted mutation baselines must observe the complete target within 100 pages, 10,000 users and **256 KiB aggregate serialized observation data**, including discarded users and target projections. This can reject large developer directories before any mutation. The local 20 MiB aggregate budget below is unchanged. Direct directory reads return one bounded page; no automatic account export, partial scan or pagination-disabled shortcut is added. These limits do not bound total process memory or client transcripts.

Preparations return the target's existing access snapshot and requested change. The engine retains a fingerprint for drift checks rather than the full target snapshot between calls. Unrelated users are transient scan inputs, excluded from mutation previews and logs. Target emails, permissions and exact authorized discovery fields reach the selected MCP client. Requests, baseline fingerprints and the original credential stay privately in the shared process-only registry until consumption or cleanup. Preparations are usable for at most ten minutes and no longer than original authorization; this is an execution deadline, not an exact deletion deadline. Periodic cleanup, in-flight work or pending disposal can retain material longer. They share other hosted workflow quotas and apply guards; restart loses them. Revocation, expiry and cleanup do not imply secure erasure or remove client copies.

Current authority is checked before every page, mutation and readback. Confirmed mutation success survives later observation failure; uncertain writes are consumed without automatic retry. No invitation acceptance or effective permission propagation is established by source coverage. Hosted adversarial, synthetic protocol/platform and controlled-runtime receipts are recorded under #61 and [verification status](validation.md#accepted-021-release-deployment-and-website-2026-10-06); no production account mutation was used for QA.

## Local review and execution

1. Call `prepare_account_access(method, parameters, body,
   acknowledge_access_effects=true)`. Delete calls omit `body`.
2. Review the targeted user's current permissions, exact proposed request and
   effects. Partial users, including owners, are refused because the caller cannot
   fully manage them. Create requires an absent target; patch/delete require a
   present target.
3. Call `apply_account_access(operation_id, confirmation)` with the exact same ID
   in both arguments. This is a review gate, not proof that a human approved it.

Preparations retain the original token privately in process memory, are single
use for ten minutes, and are lost on restart. At most 20 can be retained.
Expired preparations are purged during later preparations; no timed secure-erasure
promise is made. Apply rechecks configuration and a complete paginated baseline
with the same credential, then sends the mutation once. No credential refresh,
retry or rollback occurs. Concurrent outside clients can still change permissions
between observation and mutation; a baseline is not an atomic provider lock.

Read returns one bounded page (optional `pageSize` 1–1000) and preserves
`nextPageToken`; pagination-disabled `pageSize=-1` is intentionally refused.
Mutation baselines stop after 100 pages, 10,000 users or 20 MiB and refuse to write
if the complete account cannot be observed. Repeated tokens, duplicate users,
foreign account identities and malformed responses fail closed. URLs are fixed
Google routes with encoded path segments; redirects are disabled. Responses are
bounded at 2 MiB per request. Provider bodies, credentials and target addresses
are excluded from error messages.

A successful write reports provider acceptance separately from readback. Permission
propagation can take up to 48 hours. An invitation does not mean accepted access.
Readback failure does not erase a successful mutation; ambiguous transport or
response outcomes consume the operation ID and require reconciliation in Play
Console before another preparation. Effective access is never claimed verified.
Returned snapshots are personal account data; the MCP client controls its own
processing and retention. No account exports or credentials are written to disk.

Sparse Google ProtoJSON responses omit default false booleans and empty repeated
fields. Omitted `partial` is interpreted as false and omitted permission/grant
arrays as empty; explicit null or wrong types are rejected, and target resource
name/email must always be present. This follows [ProtoJSON default omission](https://protobuf.dev/programming-guides/json/#presence-and-default-values)
for the provider's documented gRPC-transcoded endpoint, rather than requiring
Google to emit default fields.

## Evidence and limitations

Verified with synthetic local tests for method contracts, scope rejection,
permission ceilings, complete pagination, partial users, original credentials,
expiry, single consumption, changed baselines, redacted errors and MCP tool calls.
No live privileged writes were performed. End-to-end Google permission propagation,
invitation acceptance and draft-app behavior remain unverified.

Provider contracts checked on 2026-10-06:

- [Users resource](https://developers.google.com/android-publisher/api-ref/rest/v3/users)
  describes partial users, access states and propagation delay.
- [User methods](https://developers.google.com/android-publisher/api-ref/rest/v3/users/list)
  describe pagination and account scope.
- [Grants resource](https://developers.google.com/android-publisher/api-ref/rest/v3/grants)
  describes per-app permissions and draft app identities.

The completion bundle reviews website availability and privacy claims separately;
source implementation, published package and a verified hosted endpoint are
separate states. The 0.21.0 package and controlled runtime include hosted execution; provider effects and public enrollment remain separate from inventory coverage.
