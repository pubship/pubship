# ADR 006: Local artifact transfers

Status: accepted for local implementation in v0.9.0; live upload acceptance remains unverified.

## Context

The pinned inventory includes binary uploads and downloads alongside JSON requests.
Passing entire APKs through MCP JSON would increase memory use and expose content
to the client unnecessarily. Reopening a source path after approval could upload
different bytes. A successful upload followed by a failed read must not cause a
false claim that no mutation occurred.

## Decision

Expose local-only preparation/application for five existing-edit media uploads and
externally hosted APK registration, plus a separate artifact download tool. Require
independent app and exact-method opt-ins and explicit local filesystem roots.

Use directory-relative file access, basename-only input and private immutable
snapshots bound to the original Google token and an expiring single-use preparation.
Stream bounded chunks with disk/byte/time budgets; do not buffer full artifacts or
accept arbitrary URLs. Download only from the fixed provider media route and save
under an explicit root without overwriting existing files. Filesystem side effects
must be reflected in MCP tool annotations even for provider GETs/preparations.

A shared publishing lock covers mutation preflight, dispatch and observation.
Compare available resource metadata without inventing absence from omitted lists.
For symbol files, the provider offers no byte-level baseline GET, so the limitation
is explicit. Preserve confirmed mutation and later observation as separate facts.

Support simple media transfers first. Resumable sessions, retries and hosted
filesystem operations are outside this increment. No implicit edit creation or
commit is allowed. Externally hosted APK URLs are metadata sent to Google, never
local fetch destinations.

## Consequences and verification

Prepared artifacts occupy bounded temporary disk space. Downloaded files remain
until the operator removes them. Temporary-file cleanup is not secure erasure.
Large interrupted uploads may need manual reconciliation. Google permissions,
artifact/signature correctness, app eligibility and publication are not established
by local checks or hashes.

Acceptance must cover file traversal/symlink/device/race boundaries, immutable
approved bytes, budget exhaustion, cleanup, exact media routes, no redirects,
original-token binding, uncertain outcomes and real MCP stdio behavior. Synthetic
mutation tests do not claim live Google write acceptance. Public hosting remains
unchanged. See [artifact workflows](../artifacts.md).
