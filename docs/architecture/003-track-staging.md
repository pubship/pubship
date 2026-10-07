# ADR 003: Full desired track releases with independent local authorization

Status: accepted for implementation under issue #17.

## Context

Track updates affect an array of releases, including retained version codes,
localized release notes and rollout fields. A convenience merge could drop fields
or releases. Provider responses can contain serving fallback releases, so a local
proposal cannot establish the final Google state. Existing listing/lifecycle
opt-ins must not silently gain another write capability.

## Decision

Expose a read-only full-track preview, plus local prepare/apply for PUT update.
Use an independent `GOOGLE_PLAY_TRACK_PACKAGES` subset of read packages. Require
complete explicit releases and acknowledgement of whole-track effects. Support
all pinned release fields with strict validation; preserve exact identifiers and
version-code strings. This initial decision deferred PATCH. Version 0.8 adds it
under the independent [edit resource write decision](005-local-edit-resource-writes.md),
requiring complete caller-declared arrays and fresh readback without inferred merging.

Bind the original token, exact payload, current track and edit metadata in a bounded
single-use preparation. Cap lifetime to ten minutes and edit expiry. Consume before
preflight, share the publishing lock and dispatch at most once. Return real provider
results; proposal is not a predicted after-state. No implicit lifecycle operations.

## Consequences

A user must include retained releases rather than relying on automatic merging.
The full snapshot check detects observed drift only; no provider atomic precondition
is claimed. Completed/halting and fallback behavior remain Google's authority.
Hosted Google tools stay read-only. Synthetic mutation acceptance and live read
regressions are recorded separately from real Google writes and hosted launch.

[Workflow and current provider references](../track-staging.md).
