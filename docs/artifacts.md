# Local artifact workflows

This increment covers five existing-edit uploads, externally hosted APK registration
and two generated/system APK downloads. The local setup below requires an independent app allowlist and exact method opt-in. The 0.20.0 hosted release separately enables them through `play:edit-artifacts` and `play:artifact-downloads`, explicit app consent and authenticated file handles. Hosted clients never send host filesystem paths; use [hosted transfers](hosted-transfers.md). Published and controlled-deployed 0.20.0 have separate [acceptance receipts](validation.md#accepted-020-release-deployment-and-website-2026-10-06). Existing read, listing, track
and lifecycle scopes do not grant access to local artifact files or mutations.

## Operations

All method names have the prefix `androidpublisher.`.

| Method | Behavior |
| --- | --- |
| `edits.apks.upload` | Upload an APK into a caller-owned edit. |
| `edits.bundles.upload` | Upload an Android App Bundle into an edit. |
| `edits.deobfuscationfiles.upload` | Upload mapping or native debug symbols for an existing APK or bundle version. |
| `edits.expansionfiles.upload` | Upload an APK expansion file. |
| `edits.images.upload` | Upload an image for an existing listing locale and image type. |
| `edits.apks.addexternallyhosted` | Register caller-declared externally hosted APK metadata for an eligible Managed Play app. |
| `generatedapks.download` | Save a generated APK using its explicit app, version and download identifiers. |
| `systemapks.variants.download` | Save an explicitly selected system APK variant. |

Google permissions, app eligibility, artifact validation and current size limits
still apply. Supporting the method does not prove that a particular app is eligible
for a Managed Play or system-image workflow.

## Configure and call

Add these values to your [local MCP client environment](clients.md#optional-local-artifact-workflows):

```text
GOOGLE_PLAY_PACKAGES=com.example.app
GOOGLE_PLAY_ARTIFACT_PACKAGES=com.example.app
GOOGLE_PLAY_ARTIFACT_METHODS=androidpublisher.edits.bundles.upload,androidpublisher.generatedapks.download
GOOGLE_PLAY_ARTIFACT_UPLOAD_ROOT=/absolute/path/to/approved-uploads
GOOGLE_PLAY_ARTIFACT_DOWNLOAD_ROOT=/absolute/path/to/saved-artifacts
GOOGLE_PLAY_ARTIFACT_MAX_BYTES=1073741824
```

Only enable the exact methods you intend to use. Upload preparation requires the
upload root for binary files; externally hosted metadata registration needs no
upload root. Downloads need the download root. A missing required root fails before
requesting a Google credential. Create the intended directories yourself.

Call `prepare_artifact_upload` with the method, exact parameters, selected basename,
MIME type and explicit effects acknowledgement. For example:

```json
{
  "method": "androidpublisher.edits.bundles.upload",
  "parameters": {"packageName": "com.example.app", "editId": "existing-edit-id"},
  "filename": "release.aab",
  "mime_type": "application/octet-stream",
  "acknowledge_effects": true
}
```

Inspect the returned request and snapshot metadata. Call `apply_artifact_upload`
with `operation_id` and `confirmation`, both equal to the returned ID. External
APK registration uses an explicit `body` instead of `filename` and `mime_type`.
No field, flag, region or version is silently chosen for you.

Call `download_artifact` with `method` and the exact selected artifact `parameters`.
The tool generates the local output filename rather than accepting an arbitrary
path. Obtain identifiers through existing `read_publisher` metadata methods; never
substitute a provider-returned URL for the declared method parameters.

APK MIME types are `application/octet-stream` or
`application/vnd.android.package-archive`. Bundles, symbols and expansion files use
`application/octet-stream`. Images require a concrete `image/` subtype, without
wildcards or MIME parameters. These checks do not validate artifact contents or
Google's image/content policies. Use `describe_api_method` for method-specific
parameters and `body` schema.

## File boundaries and retention

An operator explicitly configures separate upload and download roots. Input files
are single basenames inside the upload root; nested paths are not supported. The
implementation uses directory-relative file access and rejects symlinks and
non-regular files. This is a local input boundary, not an operating-system sandbox
against a process with the same account privileges.

Preparation streams the source into a private temporary snapshot and returns its
byte count and SHA-256 digest for review. Applying the preparation sends those
snapshot bytes, not a reopened source pathname. Changing the original file after
preparation cannot change the approved snapshot. The client still needs to verify
that the package, version, signing key and application content are the intended
ones; PubShip does not establish their correctness by hashing the file.

Temporary upload snapshots use owner-only access. They are released on consumption,
failed preparation, expiry cleanup or process shutdown. This is not a secure-erasure
guarantee. Downloads are saved under the configured download root and remain until
the operator deletes them. They are not extracted or executed, and are not returned
as base64 in MCP responses. Provider filenames and arbitrary returned URLs do not
determine local destination paths.

## Transfer limits

`GOOGLE_PLAY_ARTIFACT_MAX_BYTES` defaults to 1 GiB and can be explicitly raised up
to 50 GiB. Each upload is also limited by its pinned provider ceiling:

| Upload | Pinned ceiling |
| --- | --- |
| APK | 10 GiB |
| App Bundle | 50 GiB |
| Deobfuscation file | 2000 MiB |
| Expansion file | 2 GiB |
| Image | 15 MiB |

The effective upload limit is the lower of the configured limit and the method's
ceiling. Download size uses the configured limit. Size permission does not reserve
free disk space or guarantee that Google will accept the artifact. At most four
preparations are retained, with a combined snapshot budget of twice the configured
limit. Transfers stream bounded chunks rather than loading complete artifacts into
memory.

The first implementation uses one simple media request, not multipart or resumable
sessions. The socket timeout is 120 seconds and transfers have a one-hour budget
checked during streaming. Interrupted large uploads may require manual state
reconciliation; the server does not retry a dispatched mutation automatically.

## Review and mutation outcomes

Upload/registration preparations bind the selected method, package, edit,
request flags, artifact metadata, resource observation and original credential.
Confirmation is single-use, limited to ten minutes and bounded by edit expiry.
An upload that has already been dispatched is not retroactively canceled by the
preparation deadline.

Preflight compares edit metadata and the available relevant resource observation.
These are not atomic provider preconditions. Collection omissions remain unknown,
not inferred empty. Deobfuscation files have no matching GET that can establish
unchanged existing symbol bytes; the check covers the APK/bundle version collections and edit metadata only.
Image upload checks that the listing locale exists before dispatch.

A successful provider mutation and a subsequent observation are separate results.
Provider checksums are optional for APKs/bundles; a missing checksum leaves
`digest_verified` false rather than claiming a verified match. Image hash encoding
is not a documented payload-equivalence guarantee. Symbol bytes cannot be read
back with this API.
Observation failure does not undo a confirmed upload or justify resending it.
If cleanup fails after a confirmed upload or a completed download, the result can
include `cleanup_warning`. That warning does not reverse the successful operation
or claim that no file was saved. Inspect local temporary storage as appropriate.
Unknown mutation outcomes require inspection before another operation. No workflow
creates, validates or commits an edit automatically. An uploaded artifact is not a
published release, policy approval or verified signature.

Externally hosted APK registration sends caller-declared metadata, including a
package-bound HTTPS URL. PubShip does not fetch that URL or verify its remote bytes.
Do not treat registration as evidence that the URL's content matches the declaration.

## Download protocol

Both downloads use the fixed Google media endpoint, the Discovery download-service
prefix and `alt=media`. Redirects, arbitrary URL destinations and response-provided
filenames are not followed. A download changes local disk state, so the MCP tool
is not labeled read-only merely because the provider request is GET.

## Evidence and sources

Live Google mutations are not used for QA. Synthetic transport, filesystem-failure,
concurrency and MCP-client tests are separate from real-account acceptance. Consult
[verification](validation.md) and the release PR for completed checks.

Primary sources: [Google media upload](https://developers.google.com/android-publisher/upload),
[Discovery media download routing](https://developers.google.com/discovery/v1/using#media-download),
[APK upload](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.apks/upload),
[Bundle upload](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.bundles/upload),
[Generated APK download](https://developers.google.com/android-publisher/api-ref/rest/v3/generatedapks/download),
and [system APK download](https://developers.google.com/android-publisher/api-ref/rest/v3/systemapks.variants/download).
