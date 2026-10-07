# Enterprise self-hosted signing

Included in the 0.16.0 local bundle; absent from v0.15.0. The 0.21.0 package and controlled runtime add separately authorized hosted execution. Provider eligibility and signing effects remain unverified by release QA.

`prepare_signing_operation` and `apply_signing_operation` implement exactly:

- `androidpublisher.appsigning.enrollApp`
- `androidpublisher.appsigning.rotateAppSigningKey`

These are enterprise **self-hosted Cloud KMS** workflows. Standard Play App Signing
with Google-managed keys still requires Play Console. The operator must configure
an active KMS key and grant the required Google Play service identity access before
using these APIs. No tool generates, exports or manages private signing keys. The accepted contract takes public certificates and lineage; recognizable private-key encodings are rejected, without claiming arbitrary opaque input is secret-free.

## Independent local authorization

The local tools are absent by default. Configure
all three comma-separated exact scopes in the local process:

```sh
GOOGLE_PLAY_SIGNING_APPS=com.example.app
GOOGLE_PLAY_SIGNING_METHODS=androidpublisher.appsigning.enrollApp
GOOGLE_PLAY_SIGNING_KMS_VERSIONS=projects/example-project/locations/global/keyRings/example-ring/cryptoKeys/example-key/cryptoKeyVersions/1
```

Apps may be package names or decimal app IDs. They use a separate authorization
scope because an unpublished app ID need not be a package already configured for
ordinary reads. No wildcard, resource prefix, unversioned key or `latest` alias is
accepted. Credentials remain the existing local authentication mode; granting a
method locally does not grant Google-side permission or provider eligibility.

## Hosted execution: exact app/key pairs

Request `play:signing-enroll` or `play:signing-rotate` independently. Browser consent binds each exact app to a complete Cloud KMS key version for that capability. Selecting app A/key 1 and app B/key 2 never authorizes A/key 2. Enrollment does not imply rotation. Google still determines eligibility and KMS access; ordinary Google-managed signing stays in Console.

The native form offers three pairs per signing capability; the backend schema bounds both capabilities at 100 pairs combined. The larger schema limit does not expand the form. Review the validated exact authority before Google sign-in. Signing-only connections may have no ordinary read apps; they grant no account, app-store or ordinary app-read authority. Legacy/v1/v2 grants gain no signing authority on upgrade or refresh.

Inputs remain bounded JSON, including public certificate and rotation-lineage fields with existing 16 KiB decoded limits. No new upload handle, persistent file or KMS-management tool is introduced. Recognizable private-key encodings are rejected, but this is not proof that arbitrary opaque input contains no secret. Treat all supplied blobs as confidential. The exact request and original credential stay in the shared process-only preparation registry until consumption or cleanup. Execution expires within ten minutes or the original authorization deadline, whichever comes first; refresh cannot extend it. Periodic cleanup, in-flight work or pending disposal can retain material beyond that execution deadline. Cleanup, disconnect or restart removes pending state without claiming secure erasure. Raw blobs are omitted from preparation output but can remain in caller input transcripts; exact KMS resource names and hashes reach the authorized client.

Preparation and apply share connection/client, quota and single-use guards with other hosted writes. There is no provider GET baseline, readback or API rollback. Effects may be irreversible through this API. Confirming a preparation acknowledges intent, not provider validation or guaranteed future signing. No production signing or KMS change is used for QA; release and controlled-runtime receipts are tracked in #61 and [verification status](validation.md#accepted-021-release-deployment-and-website-2026-10-06).

## Local review and execution

Inspect `describe_api_method` for the pinned request. Enrollment requires exactly
one of `enrollNewApp` and `enrollExistingApp`. New-app enrollment requires a public
certificate for the KMS key; existing-app enrollment supplies its KMS reference.
An optional upload-key certificate is separate. Rotation requires the KMS key and
public certificate, an explicit non-unspecified rotation reason, and binary proof
of rotation produced by the operator's signing tooling.

Certificate fields contain canonical base64 of a single PEM X.509 public
certificate, as required by Google's bytes representation. The server parses the
certificate and computes hashes over its DER encoding. PEM private-key material
and certificate bundles are rejected. Lineage is bounded canonical base64 of
binary data; recognizable PEM and DER private-key material is rejected. Cryptographic lineage
validity, correspondence between the KMS key and certificate, and new/existing-app
eligibility are verified by Google, not inferred locally.

Preparation requires `acknowledge_signing_effects=true` and returns the method,
app, exact KMS version, enrollment mode, certificate hashes and rotation reason.
It keeps copied request bytes and the original credential in bounded process
memory; execution expires after ten minutes. Expired entries are removed during
subsequent prepare/apply calls, not by a background timer. Restart loses them.
These controls are not secure memory erasure. Raw public certificate/lineage blobs are not
returned, but the invoking client may retain its own input transcript.

After reviewing, repeat `operation_id` as `confirmation` for apply. This is not
proof of human approval. Apply consumes the ID, rechecks scopes and expiry, and
sends one request. It does not retry, refresh credentials, fall back to another
key, stage an edit or attempt rollback. Concurrent applies share the existing
process lock. Restart loses pending preparations.

## Verification limits

There is no signing GET baseline, native compare-and-swap or readback endpoint.
Do not describe a prepared operation as provider-validated. On a success response,
the server requires a SHA-256 certificate hash, validates any other returned hash
and correlates each supplied certificate to the response. Existing enrollment has
no supplied signing certificate, so the returned signing hashes are observations,
not locally verified KMS-key identity.

Malformed or mismatched responses, redirects, timeouts and provider errors leave
an uncertain outcome with the operation ID consumed. Reconcile through Play
Console before another attempt. Accepted requests do not prove propagation,
installation compatibility or successful signing of future artifacts.

Synthetic tests cover the fixed routes, all three enrollment/rotation forms,
redaction, response correlation, authorization changes, expiry and replay. No
production signing key or KMS configuration is modified for QA.

## Provider references

- [Enroll an app](https://developers.google.com/android-publisher/api-ref/rest/v3/appsigning/enrollApp)
- [Rotate a signing key](https://developers.google.com/android-publisher/api-ref/rest/v3/appsigning/rotateAppSigningKey)
- [Create a signing certificate lineage](https://developer.android.com/studio/command-line/apksigner#rotate_signing_keys_2)
