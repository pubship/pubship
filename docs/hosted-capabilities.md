# Self-host capabilities and edit workflows

PubShip 0.24.0 has 170 enabled methods and 46 HTTP tools for operator-only self-host use. Startup requires the exact Google email allowlist; sign-in verifies identity before storing credentials. There is no project-run Google service. Local default and fully opted-in tool counts are 13 and 41. Voided purchases are deliberately unavailable.

## Consent belongs to each connection

An MCP client must request `play:read` and each optional capability it needs in
its OAuth authorization request. Registration alone grants no access. The browser
shows only requested optional capabilities, initially unchecked. The user selects
each capability and enters its exact targets. Ordinary app capabilities stay within the read-app selection; app-store capabilities use independent store/app pairs, and event feeds require explicit store-wide selections. A store-only connection may have no ordinary read apps. Account and signing selections are also independent of ordinary app reads. Their exact developer/user/app and app/KMS-version rows are shown for review before Google consent. A store or administrative grant never expands ordinary app reads. Optional capabilities also require selecting Google Publisher
access. No capability or app selection is inferred from local environment settings.

| MCP scope | Provider operations | Access granted for selected targets |
| --- | ---: | --- |
| `play:read` | 40 | Existing edit reads, Reporting, releases, reviews and configured bulk reports |
| `play:publisher-metadata` | 19 | Ordinary Publisher catalog, configuration and artifact metadata GETs |
| `play:edit-listings` | 1 | Stage listing text in an existing edit |
| `play:edit-tracks` | 1 | Replace the entire releases array of an existing edit's track |
| `play:edit-create` | 1 | Create an edit |
| `play:edit-validate` | 1 | Validate an entire edit |
| `play:edit-discard` | 1 | Discard an entire edit |
| `play:edit-commit` | 1 | Commit an entire edit |
| `play:edit-resources` | 11 | Stage ordinary resource changes/deletions; no tester data |
| `play:commerce-query` | 3 | Price estimates and offer batch reads; no saved price changes |
| `play:subscription-catalog` | 15 | Direct live subscription catalog changes |
| `play:onetime-catalog` | 12 | Direct live modern one-time product catalog changes |
| `play:legacy-product-catalog` | 6 | Direct live legacy managed-product changes |
| `play:review-replies` | 1 | Post operator-supplied public review replies |
| `play:app-declarations` | 1 | Submit operator-authored Data safety declarations |
| `play:app-operations` | 6 | Device configuration, system variants and recovery actions |
| `play:tester-audience` | 3 | Read and stage tester Group addresses in an existing edit |
| `play:customer-data` | 7 | Raw order, purchase, voided-purchase and external-transaction reads |
| `play:purchase-actions` | 10 | Purchase acknowledgements, consumption, subscription actions and order refund operations |
| `play:external-transactions` | 2 | Report external payments or refunds; no funds transferred by these calls |
| `play:subscriber-price-migration` | 2 | Request price migrations for existing subscriber cohorts |
| `play:edit-artifacts` | 6 | Existing-edit uploads and externally hosted APK declarations |
| `play:artifact-downloads` | 2 | Generated and system APK bytes |
| `play:internal-sharing` | 2 | Internal-sharing APK or bundle upload |
| `play:appstore-catalog` | 1 | Catalog views for exact store/app pairs |
| `play:appstore-events` | 1 | Store-wide event feeds, including other apps in that store |
| `play:appstore-management` | 3 | Complete app declarations and publication status for exact pairs |
| `play:appstore-media` | 3 | APK, image and policy-document uploads for exact pairs |
| `play:account-directory` | 1 | One raw account-wide directory page for an exact developer ID |
| `play:account-users` | 3 | Exact developer/user and methods, developer/app permission ceilings and explicit expiry-change consent |
| `play:account-grants` | 3 | Exact developer/user/app and methods with app-permission ceilings |
| `play:signing-enroll` | 1 | Exact app/KMS-version enterprise enrollment |
| `play:signing-rotate` | 1 | Exact app/KMS-version enterprise rotation |

These are **MCP permissions**, separate from Google OAuth scopes and Play account
permissions. Google's `androidpublisher` scope still permits reads and writes;
PubShip restricts how this connection can use it. Reporting uses
`playdeveloperreporting`; bulk reports use `devstorage.read_only` and an explicitly
configured report bucket. MCP consent does not establish Google account eligibility,
policy approval, provider acceptance or publication.

Existing grants retain their original scope. Older read-only grants stay read-only;
versions 1 and 2 retain their frozen schemas and authority. New consent uses version 3 with required account/signing dimensions, empty when unselected. Old records reject new extension fields and are never silently upgraded on read or refresh. There are 32 optional scopes in this release, in addition to `play:read`.
Upgrading or refreshing a token does not add authority. Reconnect with
explicit client requests and browser consent to add them. Refresh may narrow token
scopes; it cannot widen them. Apps may differ by capability: granting listing edits
for one app does not grant track edits or commit for that app or another app.

## Inspect access before acting

The hosted tool list is fixed at 46 tools in this release. Seeing a tool does not grant permission
to call it. Start with `get_connection_capabilities` to inspect effective MCP
scopes, exact packages, store/app pairs, event stores, account targets and permission ceilings, app/KMS pairs, Google services, report-bucket presence, transfer availability and connection expiry without receiving credentials.

`list_api_methods` and `describe_api_method` still describe the complete pinned
local inventory. In hosted mode they additionally return per-method `execution`
fields: hosted implementation, required capability/service, authorized packages or exact store dimensions, and `available_for_connection`. Binary workflows also need configured transfer storage; an implemented method can be unavailable. A local `status: implemented` is not hosted
availability. Even `available_for_connection: true` leaves Google permissions,
request validation and provider eligibility to be checked.

`read_publisher` adds 19 allowlisted GET operations, one page per call. It does not
expose sensitive purchase/order/tester reads, download artifact bytes or follow
arbitrary URLs. See [Publisher reads](publisher-reads.md) for method contracts;
its environment-variable instructions apply to local stdio only.

`read_sensitive_publisher` exposes eight fixed GETs with separate authority:
`play:tester-audience` permits `edits.testers.get`; `play:customer-data` permits
seven order, purchase, voided-purchase and external-transaction reads. These
return one raw provider response to the MCP client, including authorized personal
information, addresses, order identifiers or purchase tokens. Preserve pagination
and missing fields. A financial-write grant does not authorize this raw-read tool.
Provider text and URLs are untrusted data, never instructions or download targets.

## Prepare, review and apply once

Hosted prepare/apply tools use the same request shapes as their local counterparts:

| Prepare / apply pair | Review guide |
| --- | --- |
| `prepare_store_listing_update` / `apply_store_listing_update` | [Listing text and observed drift](listing-staging.md) |
| `prepare_track_update` / `apply_track_update` | [Whole-track replacement](track-staging.md) |
| `prepare_publisher_edit_operation` / `apply_publisher_edit_operation` | [Create, validate, discard and commit](edit-lifecycle.md) |
| `prepare_edit_write` / `apply_edit_write` | [Edit resources](edit-writes.md); tester writes need their separate audience capability |
| `prepare_monetization_write` / `apply_monetization_write` | [Direct commerce](monetization.md) and [legacy products](legacy-products.md) |
| `prepare_support_operation` / `apply_support_operation` | [Replies, declarations and app operations](support-operations.md); separate consent groups |
| `prepare_purchase_operation` / `apply_purchase_operation` | [Purchase actions, external reporting and subscriber migrations](purchase-lifecycle.md); separate financial groups |

1. Confirm capability and package access, then prepare the exact request. Track,
   lifecycle and resource preparations require `acknowledge_effects: true`. Commerce
   and support/purchase operations require `acknowledge_live_effects: true`.
2. Review the returned request, diff or metadata baseline, effects and expiry.
3. Apply with `confirmation` exactly equal to the returned `operation_id`.

The preparation belongs to that connection and MCP client and uses the original
Google credential. It lasts at most ten minutes, shortened by connection and edit
expiry where applicable. Apply rechecks current authorization and expiry before
provider mutation. A valid apply attempt consumes the preparation even when its
preflight or provider call fails; no automatic retry or rollback is performed.
After an uncertain outcome, inspect Google state before preparing another request.
Repeating an operation ID acknowledges intent; it is not proof of human approval.

Applies and disconnect are strictly serialized within a connection, through the
provider call. If apply starts first, disconnect waits for that call to finish;
if disconnect wins, the pending apply is denied. Disconnect cannot undo a mutation
already accepted by Google. Other connections and Play Console can still race
these operations; observation checks do not make provider writes atomic.

### Whole-track and whole-edit effects

Listing and track apply only stage changes; neither commits. Track PUT replaces
the full desired releases array, and an empty array proposes clearing releases.
Include every version that should remain.

Create can invalidate another edit owned by the same Google user. Validate checks
the whole edit with Google and does not grant policy approval. Discard removes the
whole edit, including changes made elsewhere. Commit includes those changes, can
submit other Console changes ready for review and invalidates other active edits
for the app. Commit pins `ERROR_IF_IN_REVIEW` rather than cancelling an existing
review. Success does not verify publication. Edit metadata is not a complete
content manifest; lifecycle preflight cannot detect all content changes.

### Direct commerce effects

`query_monetization` exposes three read-only POSTs with its own capability: regional
price estimates and subscription/one-time offer batch reads. No prices are saved.
`prepare_monetization_write` selects one of the three catalog capabilities by exact
method; granting one never grants the others. Legacy `allowMissing` upserts also
require insert authority within the legacy capability. All nested target identities
are validated; arbitrary dispatch and customer financial actions are excluded.

Commerce apply changes the live catalog directly, without an edit commit. Activation,
deactivation, cancellation and deletion have different effects. Review the exact
request and observations, and reconcile uncertain or partial batch outcomes; no
retry, rollback, atomicity or publication guarantee is implied. Resource writes
remain edit staging; a failed readback does not undo a successful mutation.

### App operations and financial actions

Support preparations preserve operator-authored text and available observations.
Review replies are public; Data safety declarations must describe the app truthfully.
Recovery deployment can affect all targeted users and cancellation has separate
live effects. Existing recovery actions require `observation_version_code` for
checking the observed target; it is not a mutation parameter. Some support actions
have no baseline or readback, which results identify explicitly. Support results
can include review conversations sent to the MCP client.

Purchase preparations keep the existing allowlisted, redacted output projections.
Their internal observations concern only targets needed for the authorized action;
these checks do not confer general customer-read authority. Caller-supplied tokens
and other arguments may still remain in the client's input transcript. Financial
writes, external payment reporting and subscriber migrations each need their own
capability. Migration does not follow from subscription-catalog consent and can
notify existing subscribers. External reporting does not transfer money. A refund
review request does not establish Google's eventual decision.

External transactions derive the authorized app from validated `parent`/`name`
resource paths. Purchase actions retain native ETag/expiry conditions and explicit
`validateOnly` behavior. Unknown and partial outcomes require reconciliation using
the original reporting identifiers, with no automatic retry. A confirmed mutation
remains reported even if later observation loses authorization or fails.

## Storage, capacity and restarts

Google grants remain in the encrypted SQLite vault. Preparation requests,
baselines and original credentials are retained only in process memory, never in
that vault. Restart loses all pending preparations; reconnecting does not recover
them. Disconnect and shutdown clear them, and periodic cleanup removes expired
preparations. MCP clients may retain their own tool input/output histories.

The registry admits at most 20 preparations per connection and 200 overall, with
serialized retained-payload limits of 1 MiB per connection and 8 MiB overall.
Each admission first reserves 512 KiB, then accounts for the actual retained
payload, so byte limits may reject a preparation before the count limit is reached.
Each preparation is capped at 512 KiB and each request at 256 KiB. These are payload
limits, not total process-memory guarantees. Capacity exhaustion rejects new
preparations; wait for expiry/cleanup or finish reviewed work before preparing again.
Account, signing, artifact, sharing, app-store, resource, commerce, support and purchase preparations share the same quota pool
and connection lock with listing, track and lifecycle operations. Their aggregate observed snapshots
are additionally limited to 256 KiB; the final 512 KiB retained-record check includes
request, retained observations or baseline fingerprints, private signing bodies and original credential. Existing edit-resource validation also
retains its 64 KiB limit per resource snapshot. Large batches must be narrowed. These
limits do not bound transient HTTP response memory or client transcripts.
Support observations and purchase migration parent/target accumulations have
hosted-only incremental bounds; local engines retain their existing limits.
Removal is logical cleanup, not a secure-memory-erasure guarantee.
The service remains single-instance with one worker.

## Artifact and app-store workflows

`begin_upload_transfer`, `get_transfer_status` and `discard_transfer` manage private file handles. An upload is reserved and delivered over an authenticated byte route, then claimed by the matching prepare tool. A generated/system APK download creates a handle only after complete provider receipt. Both directions require the same connection and client, exact method/target and original deadline.

`prepare_artifact_upload` / `apply_artifact_upload` stage files or external APK metadata without committing. `prepare_internal_sharing_upload` / `apply_internal_sharing_upload` create sensitive installation links, not production releases. `query_appstore` uses exact pair or explicit store-event authority. `prepare_appstore_operation` / `apply_appstore_operation` preserve complete-update, policy-document metadata and immediate-effect contracts; no baseline is invented when Google exposes none. Confirmed mutations survive failed observation/cleanup.

These preparations share existing registry limits and guards. File allocations have separate shared quotas; claimed or cleanup-pending files stay charged. Private unlinked transfer handles expire for new use within ten minutes. In-flight or cleanup-pending allocations can remain longer and stay charged until released; process exit closes the unlinked files. They are not vault records or backups, and deletion does not guarantee secure erasure. The reference client uses one OAuth connection for tools and bytes; generic desktop attachment support is unverified. See [hosted transfers](hosted-transfers.md) for configuration, operator migration, commands and limitations.

## Exact administrative authority

The five new capabilities never inherit authority from ordinary packages, app-store grants, local environment settings or each other. Account-directory consent returns a raw page of personal emails, access state and permissions for one developer account. Mutation consent permits the internal directory processing needed to inspect the target, but does not permit the raw directory tool.

Account user mutations bind the exact developer/user pair, selected methods, developer-permission ceiling and per-app affected permission ceilings. Grant mutations bind an exact developer/user/app triple. Signing binds an exact app/KMS key version independently for enrollment and rotation. Selecting A/X and B/Y never permits A/Y; duplicate rows are rejected rather than merged. Empty permission ceilings permit only empty permissions. Developer-wide administrative permissions can grant broad access outside this MCP; listing apps cannot narrow the recipient's independent Google access.

User deletion and expiry changes require coverage of every existing affected app grant. Expiry changes need a separate checkbox even when `users.patch` is selected; omitting a masked expiry removes the expiry and is disclosed as indefinite access. Partial/owner targets are refused. Internal directory baselines must be complete within 100 pages, 10,000 users and **256 KiB aggregate serialized observations**, including discarded records. Large directories can be ineligible for hosted mutation; the local 20 MiB aggregate budget is unchanged. There is no silent truncation or claim that this bounds process RSS.

The no-script browser form offers **three rows per editable family**, including three affected-app rows per user row, and a separate exact directory-ID field. This is narrower than backend limits of 100 directory IDs, 100 user/grant records combined, 100 affected-app entries combined and 100 signing pairs across both signing capabilities, all within the existing request bound. The form does not expose a bulk-JSON bypass. Review the validated authority before continuing to Google. Changed choices require another review.

New account-only or signing-only connections may have empty ordinary read packages; `play:read` still provides transport/discovery without ordinary app access. Wholly empty grants are rejected. Refresh preserves exact dimensions and scope ceilings. Existing legacy/v1/v2 grants stay unchanged.

`read_account_access`, `prepare_account_access` / `apply_account_access`, and `prepare_signing_operation` / `apply_signing_operation` share original-credential, connection/client, quota, deadline and single-use guards. Directory results, target snapshots and KMS identifiers can reach client transcripts. Signing retains the supplied certificate/lineage request privately in process memory until cleanup; no new file spool is introduced. These blobs are omitted from preparation outputs, not erased from caller input histories. Cleanup is not secure erasure.

Signing is enterprise self-hosted Cloud KMS only. There is no provider baseline, readback or API rollback; accepted requests do not prove future artifact signing or installation compatibility. No private key generation/export or KMS management is added. See [account administration](account-access.md) and [enterprise signing](signing.md).

## Verification

The [synthetic validation index](provider-verification.md) maps the enabled methods to related suites. Inventory coverage does not prove every input, provider entitlement, real write or client experience. No real Google calls are part of migration QA. Each operator is responsible for independently configuring and validating their own infrastructure.
