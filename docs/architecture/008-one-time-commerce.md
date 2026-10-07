# ADR 008: modern one-time-product mutations

Status: local implementation published in v0.11.0 for issue #27. Live provider mutations remain unverified.
This ADR records the local design; subsequent hosted authorization is documented in
[hosted capabilities](../hosted-capabilities.md).

## Provider contract distinctions

The twelve modern product/purchase-option/offer writes reuse independent local
commerce app/exact-method permissions and the prepared direct-live-write lifecycle.
They do not gain monetary refunds, consumes or subscription price migrations.

One-time-product creation uses `patch` or `batchUpdate` with explicit
`allowMissing=true`; there is no separate create method. This is deliberate upsert
intent, distinct from subscription patch where a separate create grant exists.
For upserts show that Google ignores the mask on creation, require a complete
creation-capable body and explicit masks, and preserve allowMissing in the preview.
No implicit parameter may turn update into creation.

Purchase-option deletion supports `force=true`, which can delete child offers.
Its before snapshot does not inventory every child offer; disclose this limitation
and the cascade in the exact operation effects. Offer cancellation is preorder-only
and can cancel related pending orders. Deactivation is a discounted-offer action.
Require explicit effects acknowledgement, preserve provider eligibility, and never
call cancellation a harmless visibility toggle.

## Request and observation shape

Honor discovery's case-sensitive paths: product patch uses `onetimeproducts`, while
other modern routes use `oneTimeProducts`. Use fixed contracts, not hand-normalized
paths. Nested package/product/purchase-option/offer identities and state unions need
independent validation. Purchase-option batchDelete specifically requires different
parent products, not merely distinct options. Other batches permit distinct targets.

Products have direct GET observations. Offers lack an individual GET and require
one-item read-only batchGet observations. Keep exact raw response/status in the
baseline; do not infer empty lists from missing fields, HTTP 204 or 404. Existing
writes require positive target and parent-option observation. Explicit upserts may
retain qualified unavailable observations, with stable preflight and Google's
uniqueness/access checks; they must not claim absence proof.

Full before snapshots and comparisons remain non-atomic. Retain original credentials,
single-use IDs, bounded work/deadlines, no retry/rollback and truthful partial/unknown
outcomes. Validate each returned target and provider-guaranteed batch ordering before
claiming complete mutation success. Confirmed writes survive readback failures.

## Acceptance

Adversarial tests for all twelve methods, nested identity/union/mask boundaries,
upsert versus update, force/cancel disclosures, typed observation gaps, drift,
partial responses and original-credential single-use apply. Exercise official MCP
stdio and installed packaging. Live Tawen reads are allowed; production writes are
not part of QA. Update catalog/README/site only when implementation is verified.
