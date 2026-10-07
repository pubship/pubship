# Local edit lifecycle: create, validate, discard and commit

These four operations complete the local lifecycle around listing staging. They
are disabled by default and require a **separate opt-in**. An existing staging
configuration does not gain commit or discard access after upgrading.

## Configure the intended apps

Add these settings to the local Codex, Claude Code or generic harness environment
shown in [client setup](clients.md), then restart the MCP process:

```text
GOOGLE_PLAY_PACKAGES=com.example.app
GOOGLE_PLAY_WRITE_PACKAGES=com.example.app
GOOGLE_PLAY_EDIT_PACKAGES=com.example.app
```

Each allowlist must be a subset of the preceding one. Google permissions still
apply to the chosen account and all changes in the edit. These settings authorize
local tooling only; the hosted entry point never exposes lifecycle tools, even if
the host machine has these environment variables. It has read-only customer
consent and never falls back to the local account.

## Prepare an operation

`prepare_publisher_edit_operation` takes `action`, `parameters` and
`acknowledge_effects: true`. This acknowledgement means the client accepts the
stated scope; it is not proof that a human reviewed the change.

| Action | Parameters | Effect to review |
| --- | --- | --- |
| `create` | `packageName` | Creates an edit. Another active edit for this app owned by the same Google user can be invalidated. |
| `validate` | `packageName`, `editId` | Explicitly asks Google to validate the edit. This is not policy approval or publication. |
| `discard` | `packageName`, `editId` | Deletes the entire edit, including changes outside this process. |
| `commit` | `packageName`, `editId`; optional strictly boolean `changesNotSentForReview` | Commits the entire edit. Other changes waiting to be sent from Play Console may be included; other users’ active edits can become invalid. |

Example preparation for commit:

```json
{
  "action": "commit",
  "parameters": {
    "packageName": "com.example.app",
    "editId": "your-existing-edit-id",
    "changesNotSentForReview": false
  },
  "acknowledge_effects": true
}
```

Preparation returns the exact request and effect summary with a short-lived opaque
`operation_id`. It makes no lifecycle mutation. Existing-edit actions fetch edit
identity and expiry; create has no existing-edit preflight. Review the response
before applying it. The original access token stays privately in process memory;
apply never refreshes into a potentially different account.

### Review behavior is fixed

Commit always includes `changesInReviewBehavior=ERROR_IF_IN_REVIEW`. If Google
reports an existing review, this implementation stops. It never switches to
cancelling that review or retries with changed flags. Supplying a different review
behavior is rejected before provider I/O.

`changesNotSentForReview` defaults to `false` and is explicitly included in the
prepared request. `true` is a provider option for the rejection workflow; it is
not a general promise that a commit will never trigger review. The prepared value
cannot be changed by the apply call, and the tool never toggles it automatically.

## Apply exactly the prepared operation

```json
{
  "operation_id": "COPY_THE_RETURNED_ID",
  "confirmation": "COPY_THE_RETURNED_ID"
}
```

Call `apply_publisher_edit_operation`. The ID is consumed before preflight and can
never dispatch twice. Listing preparation IDs cannot authorize lifecycle actions.
The server rechecks local scope, preparation lifetime and existing-edit metadata,
then sends exactly one lifecycle request. It never creates a replacement edit,
implicitly validates before commit, automatically commits after validation, or
performs cleanup by deleting an edit.

Local listing, track, resource-write and lifecycle application share a lock. This prevents concurrent
mutation dispatch from these tools in one process. It does not coordinate other
processes, users or Console changes. A busy apply fails without dispatch and
consumes its preparation; inspect the result before preparing another operation.

Preparations are usable for at most ten minutes and, for existing edits, never
past the observed expiry. There are at most 100 lifecycle preparations. Consumed
entries are removed; expired entries are pruned on later preparation or process
exit. This is not a timed memory-erasure guarantee. Restart loses the IDs without
undoing any Google-side change.

## What the checks establish

The tool checks edit **identity and expiry metadata**, not every staged change.
It deliberately returns `full_edit_contents_verified: false` and
`content_drift_checked: false`. An unchanged edit ID/expiry does not establish
unchanged contents. The confirmation covers the declared whole-edit operation,
not an immutable manifest or only the last listing diff.

Use the existing edit readers and your normal release review to inspect what you
intend to commit or discard. Coordinate with other editors. Google exposes no
atomic content-revision precondition here; do not treat a metadata check as one.

A successful commit returns `committed: true` and
`publication_verified: false`. Check release lifecycle and Play Console separately.
Validation success remains a validation result, not publication. Discard removes
the edit; create returns the actual provider-generated identifier.

## Failure and recovery

After dispatch, timeouts, connection loss, redirects, invalid success responses or
provider failures are not retried. Errors expose bounded status information, not
provider response bodies or credentials. An uncertain result requires inspecting
Google state before another operation. An uncertain create may have produced an
edit whose ID was not received; creating again could invalidate it.

For an expired or invalidated edit, inspect the situation and explicitly prepare
another action if appropriate. No hidden replacement or rollback occurs. A
listing-only preview never authorizes a commit. [Track staging](track-staging.md)
and [edit resource writes](edit-writes.md) have separate opt-ins. Hosted writes,
artifact uploads and automatic release promotion remain separate work.

## Verification

Tests use synthetic provider responses and actual MCP protocol clients. Production
edit creation, deletion and commit are not used for QA. See [validation](validation.md)
for precise CI, artifact and live-read evidence; live lifecycle acceptance remains
unverified.

Sources: [Google edit lifecycle](https://developers.google.com/android-publisher/edits),
[concurrency](https://developers.google.com/android-publisher/concurrency-considerations),
[commit](https://developers.google.com/android-publisher/api-ref/rest/v3/edits/commit),
[create](https://developers.google.com/android-publisher/api-ref/rest/v3/edits/insert),
[validate](https://developers.google.com/android-publisher/api-ref/rest/v3/edits/validate),
[delete](https://developers.google.com/android-publisher/api-ref/rest/v3/edits/delete).
