# Decision: scoped existing-edit resource writes

## Context

Listing text staging, whole-track PUT and edit lifecycle have separate local
capabilities. Adding other resource writes must not widen those existing grants.
PATCH arrays have no client-side merge contract, deletion can affect collections,
and a successful write can be followed by a failed observation.

## Decision

Expose `prepare_edit_write` and `apply_edit_write` only in local servers configured
with both `GOOGLE_PLAY_EDIT_WRITE_PACKAGES` within read scope and an exact
`GOOGLE_PLAY_EDIT_WRITE_METHODS` allowlist. The whitelist contains 13 reviewed
operations, enumerated in the [workflow guide](../edit-writes.md). Hosted tools and
consent are unchanged. The necessary target-resource reads are part of the method
authorization, including private Google Group addresses for tester operations.

Prepare the exact caller body alongside the full observed resource. Require explicit
effects acknowledgement, complete intended arrays and conservative local PUT body
requirements. Do not infer merge keys, preserve omitted request values by copying,
or derive resource absence from an unsuccessful/ambiguous read. Collection baselines
require explicit arrays and unique identities; track creation requires absence.

Retain the original token, request and canonical full resource/edit-metadata
snapshots in bounded process memory. Use separate `edit_write_` IDs, a ten-minute
maximum lifetime capped by edit expiry, single-use consumption and the existing
shared publishing lock. Recheck scope and snapshots before one fixed-route mutation.
The lock remains held through a mandatory fresh readback with the same token.

Report the validated mutation response and fresh observed state separately. If
readback fails, preserve `mutation_succeeded: true` and return a safe observation
error; never forward GET's pre-write error wording. An uncertain mutation does not
trigger readback or retry. No implicit commit, rollback or replacement edit exists.

## Limits

Observed comparisons are not atomic provider preconditions. Other processes or
Console users can change state between requests. Full target-resource snapshots
are not a manifest of every staged edit change or associated imagery. Arrays express
complete caller intent without guaranteeing provider merge semantics; provider
normalization and fallback resources are preserved in actual responses.

Input, response-shape, account boundaries and protocol behavior have synthetic
coverage. Live provider mutation acceptance and publication remain unverified.
