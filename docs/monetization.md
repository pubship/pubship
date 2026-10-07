# Local subscription and offer management

Subscription and modern one-time-product capabilities perform **direct live catalog mutations**, outside publishing edits.
There is no later commit step. Activation can make a base plan or offer available
for purchase; deactivation affects new purchases while existing subscribers retain
their subscriptions under Google's rules. Delete eligibility is enforced by Google.
Price and regional changes can affect new purchases. Migrating existing subscriber
price cohorts uses separately authorized [purchase lifecycle operations](purchase-lifecycle.md).

## Read-only queries

`query_monetization(method, parameters, body)` implements regional price conversion,
subscription-offer batch retrieval and one-time-product-offer batch retrieval.
These fixed POST routes are reads, not mutations. They require ordinary local
`GOOGLE_PLAY_PACKAGES` scope, including every nested target. Conversion returns
Google's estimates and does not save any prices. Provider fields and HTTP 204 are
preserved; missing collections do not prove that no products exist.

## Opt in to live changes

In addition to `GOOGLE_PLAY_PACKAGES`, explicitly configure both:

```bash
export GOOGLE_PLAY_MONETIZATION_PACKAGES=com.example.app
export GOOGLE_PLAY_MONETIZATION_METHODS=androidpublisher.monetization.subscriptions.patch
```

The app set must be a subset of the read scope. Methods must be exact supported
IDs, not wildcards. Listing, track, edit and artifact permissions grant no commerce
write access. Hosted connections use separate query, subscription, one-time-product
and legacy-product capabilities described in [hosted capabilities](hosted-capabilities.md).
Local environment settings grant no hosted access, and there is no fallback to a
maintainer's Google credentials. The limits below describe the local workflow.

1. Inspect `describe_api_method` for the pinned provider contract.
2. Call `prepare_monetization_write` with exact parameters/body and
   `acknowledge_live_effects=true`. Read the complete snapshots and request.
3. Call `apply_monetization_write`, repeating `operation_id` as `confirmation`.
   This is an intent confirmation, not independent evidence of human approval.

A preparation is process-local, single-use and expires within ten minutes, including
preparation reads. It keeps its original bearer credential privately and does not
refresh it. Expiry prevents use; expired in-memory entries are removed on the next
prepare/apply or process exit, not by a background deletion timer. Restart loses
pending preparations. Readback shares the preparation deadline; if time runs out
after mutation, confirmed success is retained with an observation failure. At most 20 pending preparations
are retained, with at most 2 MiB of serialized baseline observations per preparation.
Applies share the publishing lock in this process. Lock contention consumes the ID.

## Supported writes and local restrictions

- Subscription create, patch, batch update and delete.
- Base-plan activate, deactivate, batch state update and delete.
- Subscription-offer create, patch, batch update, activate, deactivate, batch state
  update and delete.
- PATCH and batch updates require explicit region versions and distinct top-level
  masks. Every masked field must be supplied, including explicit clears. Unmasked
  write fields, identity masks, `*` masks and output-only fields are rejected.
- Subscription masks: `basePlans`, `listings`, `taxAndComplianceSettings`,
  `restrictedPaymentCountries`. Offer masks: `phases`, `regionalConfigs`,
  `offerTags`, `otherRegionsConfig`, `targeting`.
- `allowMissing=true` is rejected for subscriptions/offers in this section, which
  have separate create methods. Modern one-time-product upsert is separately
  supported with explicit intent; see [its workflow](one-time-products.md).
- Arrays retain exact caller intent; no merge or provider array semantics are invented.
- Single state-action bodies contain only optional `latencyTolerance`. Batch entries
  contain explicit package/product/parent identities and exactly one state action.
- Batches have 1–100 distinct complete targets. Documented outer `-` selectors are
  permitted; inner targets always name exact resources in the declared app/parent.
- Requests are finite JSON within 64 KiB. Response and observation bounds apply.
  Provider validation remains authoritative for business, regional and pricing rules.

Google's archived method remains in discovery, but Google explicitly states that
subscription archiving is unsupported. It has status `provider_unsupported`, no
callable tool and no promise of later implementation. It is not counted as working.

## Concurrency, creation and uncertain outcomes

Preparations read the complete affected subscriptions and individual offers. Apply
reads them again with the original credential and rejects any observed drift.
Base plans must be positively observed in their parent. These comparisons are not
atomic compare-and-swap: another client can race with a mutation or readback.

For creation only, an exact-resource GET returning HTTP 404 is retained as a typed
unavailable-resource observation. It does **not** prove absence or authorization.
The same observation must recur at apply; Google enforces uniqueness and access on
the explicit create POST. An observed existing create target is rejected. HTTP 204,
malformed resources and other errors cannot stand in for a valid baseline.

Each apply sends at most one mutation, with no automatic retry, refresh or rollback.
Transport errors and incomplete/mismatched response identities produce an uncertain
outcome and consume the operation ID. Batch atomicity is not assumed: reconcile
**every target** before another operation. A confirmed mutation is preserved even
when subsequent observation fails. Readback observations do not prove propagation,
purchase availability or deletion; a later 404 remains an unavailable observation.

## Provider references

- [Create subscriptions](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.subscriptions/create)
- [Patch subscriptions](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.subscriptions/patch)
- [Activate base plans](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.subscriptions.basePlans/activate)
- [Subscription offers](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.subscriptions.basePlans.offers)
- [Unsupported archive](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.subscriptions/archive)

The checked-in discovery contracts pin exact request/response schemas. Local,
synthetic, installed-package and real-account evidence are separate in
[validation.md](validation.md). No live commerce writes are part of release QA.

## Modern one-time-product extension

Twelve additional exact method opt-ins cover product/purchase-option/offer mutations.
Their explicit upsert semantics, cascade deletion and preorder cancellation differ
from subscription operations. Read [one-time-products.md](one-time-products.md) before
using them; shared expiry/concurrency/reconciliation guarantees remain unchanged.

## Legacy managed-product compatibility

Six legacy managed-product writes are separately enabled by exact method grants.
Full-update upsert additionally needs insert authority. Legacy subscription writes
are rejected; prefer modern product APIs. See [legacy-products.md](legacy-products.md).
