# ADR 007: direct local subscription and offer capabilities

Status: local implementation published in v0.10.0. Live provider mutations remain unverified.
This ADR records the local design; subsequent hosted authorization is documented in
[hosted capabilities](../hosted-capabilities.md).

## Context and decision

Commerce mutations bypass publishing edits. Existing listing/edit/track gates must
not authorize them. Use separate app and exact-method allowlists, local-only tool
registration, explicit direct-effect acknowledgement and prepared single-use apply.
The three read-only POST queries use ordinary read scope, with fixed routes and
nested target validation before credentials. No generic HTTP dispatcher is exposed.

Freeze the request, full relevant resource observations and original credential.
Use explicit field masks and region versions, reject output-only fields and
create-by-patch, preserve caller arrays and validate every batch identity. Batch
operations are bounded and cannot smuggle another package or implicit wildcard
child through an allowed parent. Unknown provider fields are preserved in readback.

## Creation baseline exception

An exact-resource 404 may indicate missing data or inaccessible data; it is never
absence proof. Store this typed observation only for explicit creates, require it
to remain unchanged at apply, and rely on Google's create uniqueness/access checks.
An observed existing target rejects create. Existing updates require an identifiable
object; HTTP 204 and omitted collections cannot be interpreted as empty resources.
Offer creation additionally requires a positively observed parent base plan.

## Concurrency and outcomes

Use full observation equality and the existing in-process publishing lock. Neither
is atomic compare-and-swap against Google. Apply consumes its ID before checking
concurrency/expiry and uses one mutation attempt. No automatic retries, refreshed
credentials, rollback or assumed batch atomicity. Validate complete response target
identities before claiming success. Preserve confirmed mutation success if readback
fails or the shared ten-minute deadline expires. Do not equate readback with propagation,
purchase availability or deletion. A post-delete 404 remains qualified.

Pending preparations are capped at 20, each with 2 MiB serialized observations;
requests are bounded to 64 KiB, provider responses to 20 MiB, HTTP calls to 15 seconds,
and work between calls to the preparation deadline. Expiry invalidates use, but
in-memory removal is opportunistic on the next prepare/apply or process exit.
This is a trusted local operator boundary, not isolation from the same OS identity.

## Unsupported provider operation

Keep subscription archive in the pinned inventory with `provider_unsupported`.
Its Google definition explicitly says unsupported, so neither implementing a
known-rejected forwarding call nor promising later support is honest coverage.

## Review and evidence

An independent architect supplied the method/identity/permission design before
implementation. Further independent agent turns were unavailable due to quota;
the implementing agent performed engineering, adversarial test and delivery passes,
and CodeRabbit reviewed the full increment. Do not represent this as four fresh
independent agent sign-offs. Synthetic MCP and transport evidence, installed-package
checks and a live read-only conversion are recorded in validation.md and the PR.
No live write or hosted commerce acceptance claim is made.
