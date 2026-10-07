# ADR 009: legacy managed-product mutation compatibility

Status: local implementation published in v0.12.0 for issue #29. Live provider mutations remain unverified.
This ADR records the local design; subsequent hosted authorization is documented in
[hosted capabilities](../hosted-capabilities.md).

Retain all six legacy mutation methods in the complete inventory, while explicitly
excluding legacy subscription writes under Google's migration guidance. Preserve
the same independent commerce app/exact-method scope and local-only preparation
lifecycle. Migrated one-time products cannot use this legacy API; the modern workflow
is required for them. Each existing observation must positively identify a managed product.

PATCH retains partial caller intent. PUT/batch update requires a complete resource,
with no hidden merge. Explicit allowMissing also requires the insert method grant
at prepare and apply, so update-only authorization does not enable creation. This
is distinct from modern one-time products, which have no separate create endpoint.
Automatic regional price conversion is preserved only when explicitly requested
and included in the effect preview; converted prices are never invented locally.

Batches validate outer, nested and resource identities; all targets are unique.
Typed unavailable observations are allowed only for explicit create/upsert and are
not absence proof. Full snapshot comparison remains non-atomic. Shared deadline,
original-token, single-use, bounded-request/response and no-retry rules apply.
Batch results must account for all target identities without assuming response order.
No legacy subscription, monetary, or hosted authority is added.

Provider contracts were read before implementation. Local adversarial tests cover
all methods and real MCP stdio with synthetic provider responses. Independent review,
platform CI, installed packaging and live read-only evidence remain separate gates.
