# Third-party app-store operations

These tools cover the eight pinned Android Publisher `appstoreappsreview`
and `appstorecatalog` methods. Access depends on Google's app-store eligibility and
account permissions; a local opt-in does not grant Google access. No real account,
Google mutation, desktop-client integration or privileged eligibility was tested.

The local setup below remains supported. The 0.20.0 hosted release uses separate catalog, management and media capabilities for exact store/app pairs, plus explicitly store-wide event permission. Store-only access grants no ordinary app reads. Media uses [authenticated transfer handles](hosted-transfers.md), not host paths. Published and controlled-deployed 0.20.0 have separate [acceptance receipts](validation.md#accepted-020-release-deployment-and-website-2026-10-06). Hosted synthetic HTTPS tests exercise all eight routes without proving live program eligibility.

## Opt-in and tools

Set all applicable variables explicitly:

```sh
export GOOGLE_PLAY_PACKAGES=com.example.store,com.example.app
export GOOGLE_PLAY_APPSTORE_STORES=com.example.store
export GOOGLE_PLAY_APPSTORE_PACKAGES=com.example.app
export GOOGLE_PLAY_APPSTORE_METHODS=androidpublisher.appstoreappsreview.createappstorehostedapp
```

`APPSTORE_STORES` and `APPSTORE_PACKAGES` must be exact subsets of
`GOOGLE_PLAY_PACKAGES`. `APPSTORE_METHODS` accepts comma-separated exact IDs; no
wildcards. Both the store and target require authorization for individual app
views and writes. The exact `recentupdateevents.list` method grant deliberately
permits the **store-wide event stream**, including apps outside target grants.
Tools are disabled by default locally. Hosted execution requires the separate
connection capabilities described above; these local variables never grant it.

- `query_appstore(method, parameters)`: one catalog GET page.
- `prepare_appstore_operation(method, parameters, body,
  acknowledge_live_effects=false, filename=null, mime_type=null)`: validate and
  review fixed intent; no Google mutation.
- `apply_appstore_operation(operation_id, confirmation)`: repeat the operation ID
  exactly after review. Confirmation is not proof of human approval.

Use `describe_api_method` for Google's exact schemas. Preparations expire after
ten minutes, are single use, retain original credentials privately and disappear
on restart. Ten preparations are allowed at a time. Apply rechecks local grants,
uses the shared publishing lock and sends once. Expiry, contention and failure
consume an applied ID. No automatic retries, rollback or credential refresh occur.
An uncertain network/provider result requires reconciliation before a new operation.

## Writes and effects

| Method suffix under `androidpublisher.appstoreappsreview.` | Effect |
| --- | --- |
| `createappstorehostedapp` | Creates the target app record before other hosted-app operations |
| `updateappstorehostedapp` | Submits complete listings, app details, active APK sets and supplied policy answers for immediate review; default state after update is PUBLISHED |
| `updateappstorehostedapppublishstatus` | Reports explicit PUBLISHED or UNPUBLISHED state on the third-party store |
| `uploadapk` | Uploads APK bytes and returns an `apkId` |
| `uploadimage` | Uploads an icon/screenshot and returns an `imageId` |
| `uploadappstoreapppolicydeclarationfile` | Uploads a supplied policy document and returns a `fileId` |

Creation, details updates and publish-state operations accept an empty successful
response. Uploads require a nonempty returned ID. Details updates require explicit
provider-required fields, nonempty required collections, unique listing languages,
unique declarations and question IDs, and one response type per policy question.
Policy answers are never inferred. Already-published APK sets require a positive
version code; supplied document expiry dates must be complete calendar dates.

These are direct operations, with no draft staging or edit commit. The pinned API
has no corresponding hosted-app baseline/readback endpoint. Catalog metadata is
not a substitute review-state observation. Results therefore report
`stale_check_performed=false`, `readback_available=false` and
`availability_verified=false`. Acceptance does not establish review approval,
publication, propagation, APK validity or truth of declarations.

Provider references: [creation](https://developers.google.com/android-publisher/api-ref/rest/v3/appstoreappsreview/createappstorehostedapp),
[details and policy schema](https://developers.google.com/android-publisher/api-ref/rest/v3/appstoreappsreview/updateappstorehostedapp),
[publish status](https://developers.google.com/android-publisher/api-ref/rest/v3/appstoreappsreview/updateappstorehostedapppublishstatus).

## Media

Set `GOOGLE_PLAY_APPSTORE_UPLOAD_ROOT` to an absolute directory and supply only a
nonhidden filename directly inside it. Regular files only: no traversal, symlinks,
hard links or devices. Preparation copies bytes to a private temporary file,
records SHA-1/SHA-256 and size, and uploads those immutable bytes even if the source
later changes. Source paths and credentials are not returned. This uses the
existing POSIX file-root implementation; Windows support is unverified.

`GOOGLE_PLAY_APPSTORE_MAX_BYTES` defaults to 64 MiB and accepts 1–10 GiB. Each upload
also obeys its pinned provider cap: APK 10 GiB, image 15 MiB, policy file 10 MiB.
Ten maximum-sized pending preparations can consume ten times the configured cap
in local temporary storage. Expired snapshots close on the next prepare/apply;
process exit also releases them. There is no background expiry worker.

APK uploads accept `application/vnd.android.package-archive` or
`application/octet-stream`; supply `body={}`. Images accept `image/png` or
`image/jpeg` locally, although Google's discovery contract advertises `image/*`;
supply `body={}`. Policy documents accept PDF, JPEG or PNG and require
`body={"fileType":"DECLARATION_FILE_TYPE_DOCUMENT"}`. A streamed multipart request
preserves both JSON metadata and binary content. Only simple media/multipart upload
is implemented; no resumable session, MIME sniffing, APK signature/package analysis
or image validation is performed. Google validates content. Upload socket timeout
is 120 seconds and transfer deadline is one hour.

Provider references: [APK](https://developers.google.com/android-publisher/api-ref/rest/v3/appstoreappsreview/uploadapk),
[image](https://developers.google.com/android-publisher/api-ref/rest/v3/appstoreappsreview/uploadimage),
[policy document](https://developers.google.com/android-publisher/api-ref/rest/v3/appstoreappsreview/uploadappstoreapppolicydeclarationfile).

## Catalog reads

`androidpublisher.appstorecatalog.recentappviews.get` requires
`appStorePackageName` and `playAppPackageName`. Returned identity must match the
requested target. Catalog data can include delivery tokens and other provider
content retained by the MCP client; URLs are returned as data and never followed.

`androidpublisher.appstorecatalog.recentupdateevents.list` requires explicit RFC3339
`startTime` inclusive and `endTime` exclusive. Nanosecond ordering is preserved.
`pageSize` is restricted locally to integers 1–1000. Each call returns one page,
preserves the provider token and does not automatically paginate. Keep other
filters unchanged when sending `pageToken`. MODIFICATION and DELETION are preserved;
deletion may mean lost catalog eligibility or removal from Play, not necessarily
that the third-party store removed the app. Responses are bounded to 2 MiB.

Provider references: [recent view](https://developers.google.com/android-publisher/api-ref/rest/v3/appstorecatalog.recentAppViews/get),
[update events](https://developers.google.com/android-publisher/api-ref/rest/v3/appstorecatalog.recentUpdateEvents/list).

## Verification and delivery

Synthetic tests cover all eight service, HTTP and MCP calls; parsed multipart
metadata and exact bytes; immutable file snapshots; scope, required fields,
ambiguous policy answers, expiry/replay, lock contention, uncertainty and redaction.
A real stdio child and official MCP client exercise preparation, apply and replay
with synthetic provider responses. These checks do not exercise Google accounts.
No dependency or discovery snapshot changes were added; existing third-party
provenance remains in `THIRD_PARTY_NOTICES.md`.

Tracked by completion bundle #38.
The parent completion PR owns shared server/catalog integration and the required
website capability/availability review. Source implementation, published package
and verified public-hosted availability are separate states.
