# Third-party materials and notices

Original project code is licensed under AGPL-3.0-only. That declaration does not
relicense dependencies, Google definitions, Google/customer data or trademarks.
Keep all applicable upstream notices with any redistributed material.

## Python dependencies

The repository locks Python dependencies in uv.lock. The environment snapshot in
[docs/dependency-inventory.md](docs/dependency-inventory.md) records installed
versions and upstream-declared license metadata. It includes development tools,
but is not a legal compatibility opinion, full binary SBOM or replacement for
upstream license/NOTICE files. Platform-specific, native bundled and container
components require their own inventory when those artifacts are distributed.

The core direct dependencies are the official MCP Python SDK (MIT), google-auth
(Apache-2.0), jsonschema (MIT) and cryptography (Apache-2.0 OR BSD-3-Clause).
Cryptography validates public signing certificates and encrypts the hosted vault.
Hosted extras add Authlib (BSD-3-Clause) and Uvicorn (BSD-3-Clause). Their
dependency trees include further licenses. Do not remove
upstream dist-info/licenses or NOTICE files from packaged environments. A wheel
of this project does not grant ownership of the dependencies installed alongside
it. Refresh the snapshot when changing the lockfile or adding hosted extras.

The optional `browser` development group adds Playwright (Apache-2.0), pyee
(MIT), and greenlet (MIT AND PSF-2.0) for intercepted, synthetic browser tests.
These are not runtime or hosted extras and are not installed in the hosted
container. Playwright's downloaded browser engines have their own upstream
notices; CI installs them as test tools, rather than bundling them in this
project's wheel or image.

## Hosted connection-page presentation

The inline PubShip wordmark is original project material, shared with the [companion website](https://github.com/pubship/website). It is not a Google logo or endorsement. The self-host connection page uses system fonts and inline assets, without external font downloads or third-party scripts.

## Google API discovery inputs and derived catalog

Source URLs, scope and modifications are recorded in [api/README.md](api/README.md).
The catalog generator records hashes and revisions; the Storage input is a scoped
subset of the official discovery document. Provider descriptions and schemas are
Google-origin material. Preserve this attribution for derived inventories.

Google Developers page licenses apply as stated on those pages. They do not by
themselves establish a blanket license for every API response. The Discovery
Service's own terms refer to Google API Terms. The exact redistribution basis for
stored discovery payloads remains an identified review item in [LEGAL.md](LEGAL.md);
this project does not claim those inputs are original project code.

## Names and data

Google, Google Play and Android are trademarks of Google LLC. Other names belong
to their respective owners. Interoperability descriptions do not imply sponsorship,
endorsement or permission to use logos. API responses and user reports remain
subject to their owners' rights and the relevant provider agreements; never put
real customer data in public fixtures or distribute it under the project license.

The separate [website repository](https://github.com/pubship/website)
records its own asset, npm-tool and container provenance.
