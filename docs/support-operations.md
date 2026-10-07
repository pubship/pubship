# Support and delivery operations

Included in the 0.15.0 local bundle; absent from earlier v0.12.0 downloads.
These local tools add eight reviewed JSON operations and two internal-sharing
uploads. The published and controlled-deployed 0.19.0 release also exposes the eight JSON operations
through customer OAuth, with three separate app-scoped capabilities:
`play:review-replies`, `play:app-declarations` and `play:app-operations`.
Existing connections gain none automatically. The 0.20.0 release adds internal-sharing uploads with separate `play:internal-sharing` consent and [authenticated transfer handles](hosted-transfers.md); its package and controlled deployment have separate [acceptance receipts](validation.md#accepted-020-release-deployment-and-website-2026-10-06). Local file-root instructions below apply to stdio only. See
[hosted consent and limits](hosted-capabilities.md).

## Enable exactly what you need locally

Keep `GOOGLE_PLAY_PACKAGES` set to your app. For JSON support operations, set:

```text
GOOGLE_PLAY_SUPPORT_PACKAGES=com.example.app
GOOGLE_PLAY_SUPPORT_METHODS=androidpublisher.reviews.reply
```

Both grants are required. Package grants must be a subset of the read allowlist;
method grants are exact, comma-separated IDs, with no wildcards. Existing listing,
edit, track or commerce grants do not authorize these operations. Google also
checks the authenticated account's actual app permissions.

| Method under `androidpublisher` | Effect | Observation |
| --- | --- | --- |
| `reviews.reply` | Publish or replace the operator-supplied reply | Exact review before prepare/apply and after the write |
| `applications.dataSafety` | Submit the operator-supplied CSV declaration | No corresponding read API; no baseline or readback |
| `applications.deviceTierConfigs.create` | Create a targeting configuration | No existing baseline; read the returned configuration ID |
| `apprecovery.create` | Create a draft remote-update action | No existing baseline; look up the returned action for a selected target version |
| `apprecovery.addTargeting` | Expand the audience | Observe the exact action before/apply/after; same criterion or expansion to all users |
| `apprecovery.cancel` | Cancel an active action; it cannot resume | Observe the exact action before/apply/after |
| `apprecovery.deploy` | Activate a draft action for all targeted users | Observe the exact action before/apply/after |
| `systemapks.variants.create` | Generate a system-image APK variant from an uploaded bundle | No existing baseline; read the returned variant ID |

## Prepare, inspect, apply once

Call `prepare_support_operation` with `method`, `parameters`, `body` and
`acknowledge_live_effects: true`. The body must be explicit, including `{}` for
cancel/deploy. Inspect `proposed_request`, `effects`, `before` and
`baseline_available`. No mutation occurs during preparation. Hosted support results may include
review conversations sent to the MCP client. They are not subject to the purchase
workflow's redacted projections; protect the returned customer text.

Existing recovery operations also require an explicit top-level
`observation_version_code`, a positive decimal string such as `"42"`. Google has
no single-action GET; PubShip lists actions for that version and requires exactly
one matching action with complete targeting and valid update timing. This local
observation parameter is never sent in the mutation body or parameters.

Call `apply_support_operation` with the returned `operation_id` repeated exactly
as `confirmation`. The preparation is bound to its copied request and original
credential, expires within ten minutes, is lost on restart and can be consumed
only once. Confirmation is not proof that a human reviewed it.

Available baselines are rechecked. Review vote counts and recovery progress
counters do not invalidate intent; changed text, targeting, lifecycle state or
update timing does. These comparisons cannot exclude races with other clients.
A competing apply, expiry or revoked local scope consumes the preparation without
performing the mutation. After an uncertain write, reconcile the target rather
than retrying automatically. No rollback or implicit publishing edit is provided.

A successful write stays successful if readback fails. `readback_succeeded: null`
and `readback_available: false` identify the declaration's unavailable read API;
that is different from a failed observation. A successful observation does not
prove propagation, public availability or the truth of a declaration.

## Content and contract limits

- Review replies are public communications, supplied by the operator. Local text
  is limited to 350 characters; Google describes its limit as approximate and
  strips HTML. Returned applied text may therefore differ from submitted text.
- Data safety CSV is preserved exactly. PubShip does not generate privacy answers,
  certify compliance or validate Google's evolving CSV semantics.
- Recovery requests require one explicit audience criterion and one version list
  or range. Deploy requires an observed draft; cancel requires an observed active
  action. Target expansion cannot update an already-all-users audience.
- Configuration/variant output-only IDs, unknown fields and ambiguous targeting
  are rejected. Variant device specifications are explicit; ABI ordering is kept.
- JSON requests are limited to 64 KiB. Observations are limited to 2 MiB; recovery
  lists to 1,000 actions. There is no recovery pagination contract. Unsupported
  continuation markers, ambiguous identities or incomplete audiences fail closed.
- Provider text is untrusted data, never instructions for the client to execute.

## Internal app sharing

Use `prepare_internal_sharing_upload` and `apply_internal_sharing_upload`. Enable
only the required artifact methods and set an explicit upload directory:

```text
GOOGLE_PLAY_ARTIFACT_PACKAGES=com.example.app
GOOGLE_PLAY_ARTIFACT_METHODS=androidpublisher.internalappsharingartifacts.uploadbundle
GOOGLE_PLAY_ARTIFACT_UPLOAD_ROOT=/absolute/path/to/reviewed-artifacts
```

Preparation accepts `method`, `package_name`, `file_path` and
`acknowledge_sharing_effects: true`. `file_path` is a non-hidden basename under the
upload root, not an arbitrary path. It freezes bytes in a private snapshot and
returns source hashes for review. Symlinks, hardlinks and nonregular files fail.
Apply repeats the operation ID as confirmation and sends those frozen bytes once
with the original credential. Changing the original file cannot change the upload.

These uploads create tester installation links directly, without a publishing
edit. They are not Production or Internal Testing track releases. Google re-signs
sharing artifacts: source hashes and returned provider hashes are reported
separately, with no equality claim. Returned URLs remain data and are never opened.
There is no separate sharing-artifact readback API.

The configured byte cap defaults to 1 GiB, with provider limits of 1 GiB for APKs
and 10 GiB for bundles. Preparations are limited to four and an aggregate of twice
the configured byte cap. Expired snapshots are purged on subsequent prepare/apply
or shutdown; this is not a timed secure-erasure guarantee. Failed cleanup retains
its reservation. No automatic retry, redirect, overwrite or production mutation
is performed for QA.

## Evidence and provider references

All new mutations are tested with synthetic transports and MCP calls. No real
review replies, declarations, recovery deployments or uploads were performed.
Google permission/eligibility and live mutation acceptance remain unverified.
See [verification](validation.md) and [the architecture decision](architecture/010-support-delivery.md).

[Review reply](https://developers.google.com/android-publisher/api-ref/rest/v3/reviews/reply) ·
[Data safety](https://developers.google.com/android-publisher/api-ref/rest/v3/applications/dataSafety) ·
[Recovery list](https://developers.google.com/android-publisher/api-ref/rest/v3/apprecovery/list) ·
[Target expansion](https://developers.google.com/android-publisher/api-ref/rest/v3/apprecovery/addTargeting) ·
[Device configurations](https://developers.google.com/android-publisher/api-ref/rest/v3/applications.deviceTierConfigs/create) ·
[System variants](https://developers.google.com/android-publisher/api-ref/rest/v3/systemapks.variants/create) ·
[Internal sharing](https://support.google.com/googleplay/android-developer/answer/9844679)
