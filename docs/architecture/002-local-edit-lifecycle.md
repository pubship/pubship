# ADR 002: Separate local edit lifecycle authority

Status: accepted for implementation in issue #15. Date: 2026-10-05.

## Context

v0.4.0 grants optional local authority to stage listing text. Creating or deleting
an edit and committing all its changes have broader effects. Upgrading must not
silently widen existing opt-in. Hosted users have consented to reads only.

## Decision

Use `GOOGLE_PLAY_EDIT_PACKAGES` within the existing write/read package scopes and
register lifecycle tools only for local services. A prepare/apply pair supports
four named operations. Every preparation requires explicit acknowledgement of its
effects, retains the original token privately, and is one-use with bounded lifetime
and capacity. Separate preparation IDs cannot authorize listing operations and vice
versa. Application shares the existing process-level mutation lock with staging.

Commit explicitly uses `ERROR_IF_IN_REVIEW`, with no cancellation fallback. Review
flags are bound in preparation. Request bodies and routes follow the pinned
provider contracts. No implicit creation, validation, retry, rollback or cleanup
mutation is permitted. Errors after dispatch state uncertainty rather than claim
that nothing changed.

Metadata checks cover edit identity/expiry. The tool does not represent a partial
set of resource reads as a complete manifest. An owned-edit registry would not
prevent external mutation either, so neither is introduced as a false safeguard.
Whole-edit authority is explicit and content verification remains the operator's
release-review responsibility. Commit can include ready-to-send Console changes;
its result never claims verified publication.

## Consequences

Default and hosted tool surfaces preserve their permissions. The lifecycle is
usable locally without inventing a generic arbitrary-request tool or a second
publishing system. Operators must coordinate edits and reconcile uncertain
results. Local serialization cannot provide atomicity against Google Console,
other users or processes. Future hosted writes require separate consent and tenant
acceptance. The broader API roadmap remains open.

## Evidence

See [workflow and provider references](../edit-lifecycle.md). Acceptance includes
wire-level contracts, scope/identity/replay/concurrency failures, actual stdio,
existing hosted regressions, package/container checks, and current website claims.
