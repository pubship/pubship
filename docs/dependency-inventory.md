# Declared dependency licenses: hosted and browser-test development snapshot

Generated 2026-10-06 from the frozen all-extras environment plus optional `browser`
dependency group using Python 3.12.13 on Darwin.
Includes installed runtime, hosted, development and browser-test dependencies;
excludes absent platform-specific packages, downloaded browser engines and
container OS components. This is declared metadata, not legal certification.

The 0.21.1 release changes only the project version in the lock; dependency versions
and the recorded license metadata remain unchanged.

Lockfile SHA-256: `29b46d22ca541bc57987559c244905a4e3907ce89832f173cd543a086500541d`.

Playwright, pyee and greenlet belong only to the optional browser-test group.
They are excluded from the hosted runtime/container dependency installation.
The browser engines downloaded by Playwright are separate test tools with their
own notices; they are not bundled in the project's wheel or hosted image.

| Distribution | Version | Upstream-declared license metadata |
| --- | --- | --- |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| Authlib | 1.8.0 | BSD-3-Clause |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| coverage | 7.16.2 | Apache-2.0 |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| google-auth | 2.59.1 | Apache 2.0 |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore2 | 2.13.1 | BSD-3-Clause |
| httpx2 | 2.13.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| joserfc | 1.7.5 | BSD-3-Clause |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| mcp | 2.3.0 | MIT |
| mcp-types | 2.3.0 | MIT |
| opentelemetry-api | 1.45.0 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| playwright | 1.63.0 | Apache-2.0 |
| pluggy | 1.6.0 | MIT |
| pyasn1 | 0.6.4 | BSD-2-Clause |
| pyasn1_modules | 0.4.2 | BSD |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic_core | 2.46.5 | MIT |
| pyee | 13.0.1 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| PyJWT | 2.15.1 | MIT |
| pytest | 9.1.1 | MIT |
| pytest-cov | 7.1.0 | MIT |
| python-multipart | 0.0.32 | Apache-2.0 |
| referencing | 0.37.0 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| rpds-py | 2026.6.3 | MIT |
| ruff | 0.16.10 | MIT |
| sse-starlette | 3.5.0 | BSD-3-Clause |
| starlette | 1.7.0 | BSD-3-Clause |
| truststore | 0.10.4 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |

Read upstream license and notice files before distribution. Bare BSD metadata does
not identify its precise variant. Native wheels, Python, downloaded browser
engines, build tooling and container operating-system packages require
artifact-level inventories. Refresh after `uv sync --frozen --all-extras --group browser`
using `importlib.metadata.distributions()` and the lockfile digest. Preserve
installed license and NOTICE files. See [notices](../THIRD_PARTY_NOTICES.md) and
[legal evidence](../LEGAL.md).
