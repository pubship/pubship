# 011: Reviewed local purchase lifecycle and billing reporting

## Scope and status

Design and acceptance plan for bundle #35.
This is a separate, version-neutral increment after support/delivery bundle #32.
The document does not establish implementation, release or live-provider acceptance.

The exact methods below have the `androidpublisher.` prefix:

| Capability | Methods |
| --- | --- |
| Purchase and order actions | `purchases.products.acknowledge`, `purchases.products.consume`, `purchases.subscriptions.acknowledge`, `purchases.subscriptions.cancel`, `purchases.subscriptions.defer`, `purchases.subscriptionsv2.cancel`, `purchases.subscriptionsv2.defer`, `purchases.subscriptionsv2.revoke`, `orders.refund`, `orders.reviewrefund` |
| External transaction reporting | `externaltransactions.createexternaltransaction`, `externaltransactions.refundexternaltransaction` |
| Subscriber price migration | `monetization.subscriptions.basePlans.migratePrices`, `monetization.subscriptions.basePlans.batchMigratePrices` |

## Authorization and lifecycle

Use three independent package/method grant sets matching these capabilities. Exact
method IDs distinguish acknowledgement from refunds, cancellation from revocation,
and catalog pricing from existing-subscriber migration. Existing catalog, support,
artifact or sensitive-read grants alone cannot authorize a new write. Customer
purchase/order/external-transaction observations additionally require the existing
sensitive-read package grant. Recheck every applicable grant before apply.

Reuse the established prepare/apply lifecycle: validated fixed routes, private
copied requests, original Google credential, ten-minute process-local expiry,
bounded preparation storage, unguessable single-use operation IDs and the shared
nonblocking apply lock. No mutation occurs merely by preparing an operation.
Confirmation repeats the operation ID; it is not proof of human approval.
Hosted service factories never register these capabilities or inherit local grants.

No automatic retry, implicit refresh, rollback or replacement identifier follows
an uncertain write. A successful mutation remains accepted when readback fails.
Represent acceptance, current observations and unavailable verification separately.
Neither an empty response nor a current resource snapshot proves settlement,
delivery of customer entitlements or completion of subscriber price migration.

Bodyless POST operations remain POSTs with no JSON body. Transport must select the
HTTP method explicitly rather than inferring it from whether a body exists.
Methods without a response schema need explicit bounded empty-success handling.
Discovery validation is supplemented by required-field, union, output-only,
cross-parent, identifier, timestamp, integer and monetary checks.

## Private intent and reviewable output

Keep exact requests and reviewed observation fingerprints private to the preparation.
Validate complete necessary observations before deriving their public projections.
The public proposal is an allowlisted projection of method, application, affected
products/items, economic action, currency/amount where available, dates, audience
and explicit policy choices. Identify redacted fields and use an opaque target
reference so repeated observations can be correlated without exposing a token.

Do not echo purchase or linked tokens, pending-refund tokens, external transaction
tokens, credential-bearing URLs, developer payloads, customer addresses or account
identifiers in proposals, exceptions, representations or diagnostics. Apply the
same projection to successful mutation responses and readbacks; redacting only
the preparation would leave a second disclosure path. Provider text is untrusted
data. No arbitrary URLs are accepted or followed.

These output controls do not promise that MCP clients conceal their input
arguments: operators still supply sensitive values. They also do not change the
separately authorized sensitive-read tool's existing raw-response contract.

## Observations and preconditions

| Operation | Observation and boundary |
| --- | --- |
| Product acknowledgement/consumption | Exact product-purchase GET before preparation/apply and after mutation. Require a completed purchase and the relevant unacknowledged/unconsumed state. Returned `productId` and `purchaseToken` may be absent; if present they must match the fixed request identity. |
| Subscription actions | Subscription-v2 GET of the exact token. Preserve item identities, lifecycle, expiry, renewal/plan information and ETag. Validate item-based revocation against observed owned items. Unknown or missing eligibility fields fail closed. |
| Orders | Exact order GET with matching order ID, financial state, amounts and relevant item identities. Refunded/unknown states cannot become an eligible refund by omission. Refund-review preference and its notification token have no corresponding GET that proves submission or token validity. |
| External creation | GET the intended transaction ID before preparation/apply. Existing resources stop creation. A 404 is a qualified unavailable observation, not proof of absence; Google enforces uniqueness. Read back the accepted identity. |
| External refund reporting | Exact transaction GET with matching package/transaction ID, state, currency and remaining amounts before preparation/apply; read back after mutation. |
| Price migration | GET each distinct subscription and validate all targeted base plans. Compare reviewed current prices/configuration before apply. Catalog observations cannot enumerate affected legacy subscribers or prove cohort migration completion. |

Subscription-v2 deferral has a provider concurrency primitive: the reviewed
`deferralContext.etag` must match the GET response and remains unchanged at apply.
Never refresh that ETag silently. Its `validateOnly` mode, if exposed, must be
labelled as a non-mutating validation and must not claim a completed deferral.
Legacy deferral similarly preserves `expectedExpiryTimeMillis`; timestamp
conversion must be exact, with no floating-point rounding of expiry boundaries.
The initial local deferral limit is one day through 365 days per request.
Other baseline comparisons are not atomic locks and cannot exclude external races.

Validate returned effects against the private preflight observation as well as
their schema: v2 deferral must account for exactly the observed subscription item
identities. External refund responses must preserve original amounts/currency and
confirm the reviewed remaining pre-tax amount after a partial refund. Inconsistent
responses leave the write outcome unknown and the operation ID consumed.

Legacy subscription cancel/defer are compatibility methods with v2 replacements.
Discovery's required `subscriptionId` conflicts with provider prose describing
optional IDs for some legacy calls. Initial support should require an explicit ID
and exactly one matching observed subscription item. Document this local add-on
limit; do not invent an omitted-ID route or silently pick one item from a bundle.
Any additional conservative lifecycle restrictions must be documented as local
support limits rather than attributed to Google without evidence.

## Financial and customer effects

- Acknowledgement and consumption affect purchase lifecycle; neither proves the
  application has granted or fulfilled an entitlement.
- Require an explicit order-refund `revoke` choice. Refund plus revocation can
  terminate access and future recurring payments. Consumed product entitlement
  handling remains the application's responsibility.
- V2 cancellation requires the intended cancellation type: stopping user renewals
  differs from stopping developer-requested payments, including installments and
  restoration. Cancellation must not be described as immediate refund/revocation.
- Revocation requires exactly one refund mode. Full refund concerns the latest
  charge on each subscription item; item-based revocation targets the selected
  product. Do not infer a precise prorated amount locally.
- Refund-review preference, consumption evidence and sample/trial declarations
  are operator supplied. Submission does not decide Google's refund outcome.
- External transaction creation/refund reports payments/refunds already handled
  outside Play. These calls do not execute the outside payment or refund. Preserve
  caller transaction/refund IDs after uncertainty; creating replacements can
  duplicate reporting. IDs must not contain personal information.
- Validate money using exact integers for `priceMicros`, with consistent currency
  and no float coercion. External partial refunds require a positive pre-tax amount
  below the observed remaining pre-tax amount. Do not invent a tax refund field.
- Subscriber migration uses current base-plan prices and notifies affected
  subscribers. Review regions, cutoff dates and requested price-increase type.
  Cohort membership/count and final customer billing remain provider controlled.

Batch price migration accepts at most 100 distinct base plans, with matching
package and parent product identities (including the documented cross-product
wildcard). Validate all nested requests before credential/provider access. Preserve
request order and verify response cardinality/order. Do not promise batch atomicity
or automatically retry an uncertain subset. Empty per-item responses establish
acceptance only; a malformed/lost batch response requires reconciliation of every
target.

## Acceptance plan

- Test all 14 exact routes, bodyless POSTs and empty responses against the pinned
  catalog; preserve method-specific effects and prose-only requirements.
- Prove exact-family and sensitive-read grants, nested-parent isolation, original
  credentials, copied intent, expiry, collisions, replay and competing applies.
- Test missing/contradictory purchase states, optional response identities, add-on
  boundaries, ETag/expiry changes and drift between preparation and execution.
- Test refund unions, explicit booleans, money/currency precision, remaining-amount
  bounds, duplicate external IDs and review-refund event/percentage limits.
- Test migration duplicate targets, mixed parents, 100/101 request boundaries,
  response cardinality, partial/uncertain outcomes and unavailable cohort readback.
- Insert synthetic secret sentinels in every sensitive request/response field and
  assert absence from all returned results, errors and object representations.
- Exercise redirect refusal, timeout, malformed/oversized provider data and
  mutation acceptance followed by failed or unavailable observations.
- Run real MCP interoperability, hosted-exclusion checks, relevant repository CI,
  package inspection and installed-package checks. Live regression uses authorized
  reads only. Do not make production financial changes for acceptance evidence.

Architecture, engineering, adversarial QA and delivery receipts must identify the
reviewed commit and remaining provider limits. Update website claims in the same
cycle. Release numbering/artifacts are frozen separately; full API coverage and
public-hosting launch are not prerequisites for this bounded local bundle.

## Initial local review receipt

The independent architecture/service pass on the #35 working tree added 26
adversarial tests in `tests/test_purchase_service_adversarial.py`; all passed after
integration. They exercise real request contracts, preflight checks, fingerprints
and redacted projections using a synthetic provider transport, including one real
MCP tool call. The review identified and verified fixes for deferral response item
correlation and external refund original-amount/currency correlation. Owned test
lint/format checks and the working-tree whitespace check passed.

This receipt covers the local working tree, not a frozen commit or release.
Full-suite/platform CI, artifact acceptance and provider eligibility remain separate
checks. No live financial or customer mutation was performed.

## Provider evidence

- [Subscription-v2 deferral and ETag](https://developers.google.com/android-publisher/api-ref/rest/v3/purchases.subscriptionsv2/defer)
- [Legacy deferral precondition](https://developers.google.com/android-publisher/api-ref/rest/v3/purchases.subscriptions/defer)
- [Cancellation modes](https://developers.google.com/android-publisher/api-ref/rest/v3/purchases.subscriptionsv2/cancel)
- [Revocation refund modes](https://developers.google.com/android-publisher/api-ref/rest/v3/purchases.subscriptionsv2/revoke)
- [Order refund](https://developers.google.com/android-publisher/api-ref/rest/v3/orders/refund)
- [Refund preference and evidence](https://developers.google.com/android-publisher/api-ref/rest/v3/orders/reviewrefund)
- [External billing reporting](https://developer.android.com/google/play/billing/outside-gpb-backend)
- [External partial refunds](https://developers.google.com/android-publisher/api-ref/rest/v3/externaltransactions/refundexternaltransaction)
- [Subscriber migration](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.subscriptions.basePlans/migratePrices)
- [Batch migration](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.subscriptions.basePlans/batchMigratePrices)
- [Subscription API deprecations](https://developer.android.com/google/play/billing/play-developer-apis-deprecations)
