# Legacy managed-product compatibility

Six legacy `androidpublisher.inappproducts` mutations are implemented: `insert`,
`patch`, `update`, `delete`, `batchUpdate` and `batchDelete`. Prefer the modern
[one-time-product workflow](one-time-products.md) for new integrations. Google
states that these legacy endpoints should no longer be used for subscriptions.
Google also states that a product migrated to the modern one-time-product model
cannot be accessed through `inappproducts`. Compatibility here is restricted to
eligible, unmigrated managed products. Use the modern workflow for migrated apps;
repeated legacy calls or broader credential grants do not reverse migration.
The local implementation rejects subscription resources and requires observed
existing targets to have `purchaseType=managedUser`.

Use the existing local `prepare_monetization_write` / `apply_monetization_write`
tools with independent commerce app and **exact legacy method** grants. These are
direct live catalog operations; no later publishing-edit commit is needed. They
are not available to hosted sessions and do not grant monetary actions.

## Intent and permissions

- `insert` is explicit creation. An already observed target is rejected.
- `patch` retains its partial body; the tool never merges missing values itself.
- `update` and `batchUpdate` send complete caller-declared resources. Full writes
  require explicit managed type, active/inactive status, default language with a
  matching listing and a positive default price.
- `allowMissing=true` remains explicit. For a full update that may create a missing
  product, also grant `androidpublisher.inappproducts.insert`. This additional grant
  is checked before credentials and again at apply. An update-only grant cannot
  silently create a product. No flag is automatically inserted.
- `autoConvertMissingPrices=true` asks Google to generate prices for targeted regions
  without explicit prices. This effect is included in the preparation. Google
  chooses the converted values; the preview does not fabricate them.
- Active/inactive status and prices can affect live purchase availability. Deletion
  is subject to Google's eligibility rules. Inspect the exact target and effects.

Every nested package/SKU must match its declared parent. Batches contain 1–100
unique targets; raw SKUs are bounded to 200 characters locally. Supplied prices use
positive bounded decimal `priceMicros` strings and three-letter currency codes.
Subscription fields are rejected. Provider business and regional validation remains
authoritative; passing local validation does not establish provider acceptance.

## Observation and failure behavior

Before and immediately before apply, read each exact managed product with its
fixed GET route and retain the full response. Existing targets need matching
app/SKU/type identities. Creates and explicitly authorized upserts may retain a
typed HTTP 404 as an unavailable observation, not proof of absence or permission.
HTTP 204 and malformed data cannot be substituted for an empty baseline.

The shared original-credential, single-use, ten-minute preparation and bounded
snapshot rules apply. Concurrent provider changes can still race with a mutation;
these comparisons are not atomic. One mutation attempt is sent, with no retry or
rollback. Complete response identity sets must match all batch targets before
claiming success; response ordering is not invented when the provider does not
promise it. Confirmed success is preserved if readback fails. Reconcile every
target after an uncertain/partial batch outcome before another operation.

## Live verification boundary

The installed candidate's legacy catalog read on the owner-authorized Tawen app
returned HTTP 403 with Google's message, "Please migrate to the new publishing API."
Modern catalog and exact product reads succeeded for the same app during the
preceding increment. This is consistent with Google's documented migration
restriction; it does not establish that every legacy catalog is unavailable.
No successful live legacy read or mutation is claimed. Legacy workflow behavior is
verified against synthetic provider boundaries, and HTTP 403 remains a failure.
The tool does not silently switch APIs or attempt a write after failed observation.

## Provider references

- [One-time-product migration and legacy access restrictions](https://support.google.com/googleplay/android-developer/answer/16430488?hl=en)
- [Insert](https://developers.google.com/android-publisher/api-ref/rest/v3/inappproducts/insert)
- [Full update](https://developers.google.com/android-publisher/api-ref/rest/v3/inappproducts/update)
- [Batch update](https://developers.google.com/android-publisher/api-ref/rest/v3/inappproducts/batchUpdate)
- [Google's subscription migration notice](https://android-developers.googleblog.com/2023/06/changes-to-google-play-developer-api-june-2023.html)

No live product mutation is part of QA. See [validation](validation.md) and the
release PR for synthetic, MCP, package and real-read evidence separately.
