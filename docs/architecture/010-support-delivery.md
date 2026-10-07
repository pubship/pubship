# 010: Reviewed local support and internal sharing

## Decision

Add an independent support-operation family for eight direct JSON mutations and a
separate sharing-upload family for the two media operations in bundle #32. Keep
all new capabilities local. Hosted service factories do not register them.

The support service follows the existing original-credential, frozen-intent,
single-use preparation design, but does not borrow commerce effects or pretend
all methods have an existing-resource baseline. Sharing reuses the established
private file snapshots and bounded upload transport without the edit lifecycle.

## Trust and lifecycle boundaries

Independent exact app/method grants authorize support operations. Sharing uses the
artifact scopes with two separate exact IDs; adding those IDs does not expose them
through the edit-bound upload tool. Scopes are rechecked before a write. No public
service or tenant receives these local grants implicitly.

Preparations expire within ten minutes and use unguessable operation IDs. Original
credentials remain private; provider errors never echo bodies, URLs or credentials.
One shared nonblocking apply lock coordinates publishing families. Uncertain
outcomes consume the operation; no automatic retries or credential switching.

Review and existing-recovery preparations preserve full observations, but compare
intent-relevant fingerprints. Review vote counts and recovery progress counters
are excluded. Text, target criteria, status and update timing remain protected.
These are observations, not atomic compare-and-swap guarantees.

## Provider-specific observations

Review replies observe the exact review using the retained credential. Google
normalizes reply HTML, so mutation acceptance does not require byte-for-byte echo.
Recovery has no action GET: a separate local version code scopes its list, and
exactly one action must match. Incomplete audience data, changed state, duplicate
IDs, unexpected continuation or invalid timing fail closed. The mutation never
receives the local observation parameter. Deployment accepts only observed drafts;
cancellation only observed active actions. Expansion to all users is supported,
but an already-all-users audience cannot expand.

Creates have no existing-resource baseline. Device configurations and system
variants read back by returned IDs; recovery creation reads the selected version
and returned action ID. Data safety has no read API. Unknown readback, failed
readback and confirmed mutation are represented separately.

Sharing creates tester links directly. Google re-signs uploads, so the frozen
source hash and provider artifact hash have distinct meanings. Returned links are
never fetched. The server retains byte/count reservations until cleanup succeeds;
operation-ID collision handling cannot replace an earlier preparation.

## Acceptance evidence

Architecture review checked current official contracts and existing code before
implementation. Independent engineering/QA found and corrected all-users expansion,
incomplete observed targeting, malformed update timestamps and sharing-ID collision
handling. Synthetic tests cover exact routes, bounded files, scopes, single-use
confirmation, expiry, drift, uncertain responses, observation failure and actual
MCP tool calls. Live mutation acceptance remains unavailable by design: production
state is never changed to establish QA evidence.

See [workflow details and current verification](../support-operations.md). Release
number, package acceptance and public hosted acceptance are separate decisions.
