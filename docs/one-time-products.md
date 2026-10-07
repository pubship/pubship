# Modern one-time-product changes

Twelve additional methods use the existing local `prepare_monetization_write` and
`apply_monetization_write` tools. Enable only the exact method IDs you need in
`GOOGLE_PLAY_MONETIZATION_METHODS`, with the independent app scope inside ordinary
read access. Old subscription method grants do not authorize these new methods.
Hosted sessions expose none of these commerce tools.

## Supported methods

Under `androidpublisher.monetization.onetimeproducts`:

- Product `patch`, `batchUpdate`, `delete` and `batchDelete`.
- Purchase-option `batchDelete` and `batchUpdateStates`.
- Offer `activate`, `deactivate`, `cancel`, `batchDelete`, `batchUpdate` and
  `batchUpdateStates`.

These are direct live changes without a later publishing-edit commit. They do not
grant refunds, consumes, external transactions or subscriber cohort migration.

## Effects to review

`allowMissing=true` is an explicit upsert: Google can create the product/offer when
unavailable and ignores the update mask for a newly created resource. There is no
separate modern product create endpoint. Unlike subscription patch, explicit
one-time upsert is supported. The preview preserves this flag and requires a
creation-capable body, explicit region version and explicit mask. It never silently
adds allowMissing. Existing targets are updated, so this is not create-only intent.

`force=true` on purchase-option deletion can also delete its child offers. The
preview states this cascade and does not claim to inventory all child offers.
Provider eligibility still applies. Purchase-option batch deletion requires distinct
parent products, according to the pinned provider contract.

**Canceling a preorder offer can cancel all associated pending orders.** It is not
just hiding the offer. Deactivation is a discounted-offer transition; activation,
preorder cancellation and deactivation remain subject to Google's state/type rules.
These effects appear in the preview and require the same explicit live-effects
acknowledgement. No operation is run automatically after preparing it.

## Input and observation rules

- One-time product IDs are bounded locally to 200 characters; purchase-option and
  offer IDs to 63. Use raw IDs, not percent-encoded paths.
- Batches contain 1–100 distinct complete targets. Every nested app/parent identity
  must match the declared parent; documented outer wildcards never authorize inner
  wildcard targets. State requests choose exactly one action.
- Explicit top-level product masks: `listings`, `purchaseOptions`, `offerTags`,
  `taxAndComplianceSettings`, `restrictedPaymentCountries`.
- Offer masks: `discountedOffer`, `preOrderOffer`, `gameRewardOffer`, `offerTags`,
  `regionalPricingAndAvailabilityConfigs`. Output-only state and region-version
  resource fields cannot be sent as write intent. No wildcard mask or hidden merge.
- Upserts include nonempty localized listings and purchase options, or exactly one
  offer type. Purchase options require exactly one buy/rent type. Provider business,
  region, pricing, dates and eligibility validation remains authoritative.
- Parent products use fixed GET paths; offers use their supported one-item read-only
  batchGet POST. There is no invented individual offer GET. Provider path casing is
  preserved, including the lowercase product patch route.
- Existing writes require positive matching target and purchase-option observations.
  Explicit upserts can retain a typed unavailable response or omitted/empty offer
  collection without treating it as absence proof. Raw status/response is frozen,
  compared again and never rewritten to fabricate an empty catalog. HTTP 204 is not
  a usable baseline. Google enforces upsert access and uniqueness.

The shared preparation lifecycle binds the original credential, exact request and
bounded observations for single-use apply within ten minutes. Full comparisons
are not atomic compare-and-swap. No automatic retry, rollback or assumed batch
atomicity. Validate every returned target in Google's documented order before
claiming complete success; empty batch-deletion responses are interpreted according
to the pinned no-response contract. Confirmed success survives readback failure.
See [the shared commerce workflow](monetization.md) for limits and expiry/retention.

## Provider references

- [Product patch/upsert](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.onetimeproducts/patch)
- [Purchase-option deletion](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.onetimeproducts.purchaseOptions/batchDelete)
- [Offer updates](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.onetimeproducts.purchaseOptions.offers/batchUpdate)
- [Offer cancellation](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization.onetimeproducts.purchaseOptions.offers/cancel)

No production write is performed for QA. Synthetic execution, protocol/package
checks and live metadata reads are separate evidence in the release PR.
