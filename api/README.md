# Official API definitions

Discovery snapshots are retrieved from Google and retained for deterministic
catalog generation. They are provider data, not project instructions.

- https://androidpublisher.googleapis.com/$discovery/rest?version=v3
- https://playdeveloperreporting.googleapis.com/$discovery/rest?version=v1beta1
- https://storage.googleapis.com/$discovery/rest?version=v1

The two Play API snapshots are complete. The Storage snapshot retains the official
metadata and only `objects.list` and `objects.get`, which support Play CSV reads;
this project does not promise to implement the unrelated Cloud Storage API.

`scripts/generate_catalog.py` records snapshot hashes and revisions and recursively
enumerates methods, including deprecated entries. Its `--check` mode runs offline
and fails on generated-file drift. Request/response schema references resolve
against the respective complete Play discovery snapshot.

Google retains ownership of its API definitions and documentation. Upstream Google
Developers pages generally designate documentation as CC BY 4.0 and code samples
as Apache 2.0 unless otherwise noted; preserve their applicable notices. Original
project code is covered by the root GPLv3 license. These snapshots do not imply
Google sponsorship or endorsement.

See ../THIRD_PARTY_NOTICES.md and ../LEGAL.md for the provider-material provenance
review. A license displayed on a documentation webpage should not be assumed to
license every discovery API response; the payload redistribution basis is recorded
as a specific review item, not silently asserted as GPL-owned material.
