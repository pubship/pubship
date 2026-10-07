# Purchase lifecycle and customer actions

This local capability ships in the 0.16.0 supported-inventory bundle, separately
from public hosting. It covers 14 fixed methods, with independent
permissions for three kinds of operation. Do not enable a family merely to read
a catalog. New mutation checks are synthetic; no customer payments, refunds,
subscription changes or migrations are performed for QA.

## Enable only the local operations you intend to use

All app names must also appear in `GOOGLE_PLAY_PACKAGES`.

| Family | Package grant | Exact method grant |
| --- | --- | --- |
| Purchase/subscription and order actions | `GOOGLE_PLAY_PURCHASE_PACKAGES` | `GOOGLE_PLAY_PURCHASE_METHODS` |
| External transaction reporting | `GOOGLE_PLAY_EXTERNAL_TRANSACTION_PACKAGES` | `GOOGLE_PLAY_EXTERNAL_TRANSACTION_METHODS` |
| Existing-subscriber price migration | `GOOGLE_PLAY_PRICE_MIGRATION_PACKAGES` | `GOOGLE_PLAY_PRICE_MIGRATION_METHODS` |

The first two families additionally require `GOOGLE_PLAY_SENSITIVE_READ_PACKAGES`
for their customer-record observations. Existing catalog-write, support, edit or
artifact grants never imply these permissions. Values are comma-separated exact
identifiers, without wildcards. Granting a method locally does not create Google
Play account permissions or program eligibility.

For example, enabling **only** acknowledgement of a completed one-time purchase:

```sh
export GOOGLE_PLAY_PACKAGES=com.example.app
export GOOGLE_PLAY_SENSITIVE_READ_PACKAGES=com.example.app
export GOOGLE_PLAY_PURCHASE_PACKAGES=com.example.app
export GOOGLE_PLAY_PURCHASE_METHODS=androidpublisher.purchases.products.acknowledge
```

Keep authentication and any customer tokens outside repository files. Follow
[client configuration](clients.md) for Claude Code, Codex and a generic stdio
harness. Published and controlled-deployed 0.19.0 adds them with explicit `play:purchase-actions`,
`play:external-transactions` or `play:subscriber-price-migration` consent for exact
apps. Hosted internal observations are limited to the authorized action's targets;
they do not grant the separate `play:customer-data` raw-read tool. Local environment
settings grant no hosted authority. See [hosted limits](hosted-capabilities.md).

## Review, then execute once

`prepare_purchase_operation(method, parameters, body,
acknowledge_live_effects=true)` validates supplied intent and reads the current
resource with the original credential. It returns the effects, redacted intent
and observation, an expiring `operation_id`, and whether the requested v2 deferral
is validation-only. Preparation does not mutate Google data.

`apply_purchase_operation(operation_id, confirmation)` requires the exact same ID
as confirmation. Confirmation is a protocol acknowledgement, not evidence that a
human reviewed it. The server rechecks grants and the observation, preserves any
supplied native ETag/expected-expiry precondition, and sends the frozen request
once. IDs are single use, expire after ten minutes, are bounded to 20 per process
and are lost on restart. Credentials are never refreshed during an operation.

Request inputs are bounded by the strict contract validator. The discovery schema
is a guide, not a promise that arbitrary provider fields are accepted: local
validation deliberately rejects unknown/output-only fields, incomplete intent
and unsupported legacy shapes. Use `describe_api_method` and the method-specific
errors to inspect the contract before preparing.

Purchase tokens, pending-refund/external transaction tokens, developer payloads
and customer addresses stay in the private request/observation rather than output
previews or exception text. **The caller's MCP client can still retain its input
arguments in conversation history or logs.** Process-local expiry is not a secure
memory-erasure guarantee.

PubShip 0.24.0 includes 170 enabled methods. Voided-purchases access is disabled and Google subscription archiving is unsupported. Tool availability is not proof of live provider execution.

## Operation effects and limits

All method IDs below have the `androidpublisher.` prefix.

| Operations | Reviewed effect and observation |
| --- | --- |
| `purchases.products.acknowledge`, `.consume` | Acknowledge a completed purchase or consume its entitlement. Inspect the exact purchase first; already acknowledged/consumed or pending purchases are not treated as fresh actionable purchases. |
| `purchases.subscriptions.acknowledge`, `.cancel`, `.defer` | Legacy compatibility with an explicit subscription ID and one matching subscription line item. Use v2 for supported modern cancellation/deferral workflows. |
| `purchases.subscriptionsv2.cancel` | Cancellation type is explicit because customer-requested and developer-requested cancellation have different renewal, installment and restoration effects. |
| `purchases.subscriptionsv2.defer` | Retains the reviewed ETag. Explicit `validateOnly` is distinguished from a mutation in the result; it does not silently become execution. |
| `purchases.subscriptionsv2.revoke` | Revokes access using one explicit refund mode. Full refund and item-based refund have different scope. |
| `orders.refund` | Explicit refund and revoke choice. Revocation can terminate access and future subscription payments. |
| `orders.reviewrefund` | Submits the operator's refund preference and supporting evidence. Acceptance is not proof of Google's eventual decision; no readback establishes that submitted preference. |
| `externaltransactions.createexternaltransaction`, `.refundexternaltransaction` | Reports a payment or refund already handled outside Play. It does **not** transfer funds. Exact currency, integer monetary amounts, supplied timestamps and transaction/refund identifiers remain part of the reviewed intent. |
| `monetization.subscriptions.basePlans.migratePrices`, `.batchMigratePrices` | Migrates existing subscribers toward the selected current base-plan prices and can notify them. A catalog snapshot cannot enumerate the affected cohort or prove completion. Batches preserve explicit item order and cannot be assumed atomic. |

External-create GET returning 404 is an unavailable observation, not proof that a
resource does not exist or that creation is authorized. Google enforces identifier
uniqueness. Keep the original intended external transaction/refund identifier when
reconciling an uncertain outcome; a fresh identifier can duplicate reporting.

Observations are not atomic locks. Native ETag and expected-expiry controls are
retained where the API supplies them; other clients can race the remaining
operations. There is no automatic retry, rollback, inferred batch atomicity,
implicit publishing-edit commit or guarantee of downstream completion.

### Local eligibility bounds

These checks deliberately support a narrower set of actionable observations than
every state Google might accept. They are local limits, not a complete statement
of Google eligibility; provider permissions, timing and program rules still apply.

- Product acknowledgement and consumption require an explicitly completed purchase
  and the relevant acknowledgement/consumption state still unset. Optional returned
  product and purchase-token identities must match when present.
- All legacy subscription actions require exactly one matching subscription item.
  Subscription acknowledgement requires `ACTIVE` with acknowledgement pending.
  Deferral requires `ACTIVE` and every affected item unexpired. Legacy expected
  expiry must match the observed timestamp exactly, without floating-point or
  sub-millisecond truncation. Both deferral APIs accept an extension from one
  through 365 days; v2 retains the caller's exact observed ETag.
- Cancellation supports `ACTIVE`, `IN_GRACE_PERIOD`, `ON_HOLD` and `PAUSED`, with
  auto-renewing items and at least one explicitly enabled renewal. Revocation
  supports those states plus `CANCELED`; every selected item must have an observed
  successful charge. An item-based refund must identify an owned item. Terminal
  and pending purchase states are rejected.
- Order refunds accept `PROCESSED` or `PARTIALLY_REFUNDED`; refund-review submission
  also accepts `PENDING_REFUND`. The order GET does not validate the notification's
  pending-refund token or prove the submitted preference. Google decides refund
  eligibility, including order-age restrictions.
- External refund reporting requires `TRANSACTION_REPORTED`. A partial pre-tax
  refund must use the observed currency and be strictly positive and below the
  remaining pre-tax amount, using exact integer micros. Returned original amounts
  must remain unchanged, and the returned remainder must match the reviewed
  deduction; concurrent changes can therefore yield an uncertain result.
- Price migration requires an `ACTIVE` or `INACTIVE` renewable or installment base
  plan and an explicit current destination price for every requested region.
  Prepaid plans are outside this local workflow. Batches contain 1–100 distinct
  base plans and read each distinct product once; expiry stops further requests.

Deferral responses must cover the same product items as the reviewed subscription.
No missing item or changed refund amount is silently interpreted as success.

## Interpreting results

Provider acceptance and readback are separate fields. A successful write followed
by failed readback remains an accepted write with `readback_succeeded=false`.
Missing readback is reported as unavailable, not fabricated. Successful explicit
validation reports `validation_only=true`, `validation_succeeded=true` and
`mutation_succeeded=null`.

A timeout, malformed response or uncertain transport result consumes the ID.
Reconcile the affected record before preparing another mutation. Do not retry
based only on a missing response or create new transaction identifiers to escape
an error. Provider text is untrusted data, never an instruction to the agent.

See [the architecture and provider references](architecture/011-purchase-lifecycle.md)
and [validation evidence](validation.md). Public hosting has separate acceptance;
API implementation coverage does not establish a production hosted service.
