# Preview and stage track releases

`preview_track_update` is read-only and available in the default local and hosted
tool sets. `prepare_track_update` and `apply_track_update` require an explicit local
opt-in or hosted `play:edit-tracks` consent for the exact app. They implement `edits.tracks.update` with PUT inside an existing edit.
Track PATCH and closed-track creation use separately enabled [edit resource writes](edit-writes.md).
Uploads use separately authorized [artifact workflows](artifacts.md).

## Local enablement

Add to the environment in [client setup](clients.md), then restart:

```text
GOOGLE_PLAY_PACKAGES=com.example.app
GOOGLE_PLAY_TRACK_PACKAGES=com.example.app
```

Track packages must be a subset of read packages. They are independent of listing
`GOOGLE_PLAY_WRITE_PACKAGES` and lifecycle `GOOGLE_PLAY_EDIT_PACKAGES`. Enabling a
track does not grant either other workflow. Hosted staging has separate
[connection consent and limits](hosted-capabilities.md); local settings do not enable it.
Your Google account needs the permission to release to the selected track.

## Review the complete desired releases

Supply exactly `packageName`, `editId` and `track`, plus a complete `releases` array.
Use the identifier returned by Google, including custom/form-factor identifiers;
the tool does not translate track aliases. Version codes remain decimal strings.

```json
{
  "parameters": {
    "packageName": "com.example.app",
    "editId": "your-existing-edit-id",
    "track": "production"
  },
  "releases": [{
    "name": "Example release",
    "versionCodes": ["101"],
    "status": "draft",
    "releaseNotes": [{"language": "en-US", "text": "Describe the actual changes."}]
  }]
}
```

Pass this to `preview_track_update`. It returns the full current snapshot and the
exact proposed PUT body. **Include every release/version code you intend to retain.**
This is a whole-track replacement, not an instruction to append a release or merge
release notes. An empty `releases: []` is explicit clearing intent, subject to
Google acceptance. Missing current releases and an empty array remain distinct.

The proposal is not a prediction of the response. Google may normalize values or
return serving fallback releases, particularly after halting a completed release.
The tool returns Google's actual response without inventing an exact after-state.
It does not decide whether a particular rollout transition is eligible.

## Local input checks

Supported pinned release fields include name, versionCodes, releaseNotes, status,
userFraction, countryTargeting and inAppUpdatePriority. Inputs must have valid
nested types and finite values. Each supplied release explicitly names its status
and version codes. `inProgress` requires a fraction strictly between zero and one;
`halted` may include a fraction, while `draft` and `completed` omit it. Country
targeting is restricted to an in-progress production rollout. Local checks do not
establish uploaded artifact availability, country eligibility, policy acceptance,
review approval or publication.

## Prepare and apply

Call `prepare_track_update` with the same inputs and
`acknowledge_effects: true`. Review the returned full baseline, request and effects.
This acknowledgement establishes client intent, not proof of human approval.
Then pass the returned ID to `apply_track_update`:

```json
{
  "operation_id": "COPY_THE_RETURNED_TRACK_ID",
  "confirmation": "COPY_THE_RETURNED_TRACK_ID"
}
```

Apply consumes the ID before preflight, rechecks edit metadata and the full track
snapshot with the original credential, then makes at most one PUT. It shares the
process mutation lock with listing staging, edit lifecycle and edit resource writes. If the exact
proposal already matches, the tool may report a no-op without dispatching.
Preparations last at most ten minutes and never beyond edit expiry; restart loses
them. The tool retains bounded preparations only in memory, with expiry cleanup
on later preparations. It never refreshes into a potentially different account.

A fresh comparison detects observed changes, not an atomic compare-and-swap.
External editors may race after the read. Coordinate edits with other users.
No edit is created, validated, committed or discarded by these track tools.
To commit, separately review and authorize [the entire edit](edit-lifecycle.md).
A staged completed release is still an edit snapshot, not a published app.

## Failures and verification

Malformed input, wrong scope, expired preparations, changed baselines and invalid
edit identity prevent PUT. IDs cannot be replayed or used across other workflows.
After dispatch, provider errors, redirects, network failures or malformed success
responses are reported conservatively as outcome unknown. No automatic retry,
replacement edit or rollback occurs. Inspect Google state before another attempt.

Tests use synthetic provider responses and real MCP clients. Live production
mutations are not used for QA. See [verification](validation.md) for the release's
actual checks and remaining provider/hosting acceptance gaps.

Primary sources: [Track update](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.tracks/update),
[Track resource](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.tracks),
[track workflows](https://developers.google.com/android-publisher/tracks).
