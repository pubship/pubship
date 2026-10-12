# Distribution

Package: `pubship`. Registry namespace: `io.github.pubship/pubship`. Release versions are declared in `pyproject.toml` and `server.json`; verify the published version independently. Repository: `pubship/pubship`. License: AGPL-3.0-only.

Source preparation is not publication. A maintainer must create the release and approve the protected publishing environments. Never approve your own deployment or review. PyPI and registry publication must use the same accepted release bytes.

## PyPI Trusted Publishing

- Owner: `pubship`
- Repository: `pubship`
- Workflow: `publish-pypi.yml`
- Environment: `pypi`

The workflow validates the accepted commit, CI and release checksums before Trusted Publishing. Configure a human required reviewer and main-only deployments. No package token belongs in source.

## MCP Registry

Use `publish-mcp-registry.yml` with the `mcp-registry` environment after PyPI succeeds. The manifest namespace is `io.github.pubship/pubship`, using GitHub OIDC. Check the active registry entry and published package version afterward. No submission is implied by repository metadata.

## Verified 0.24.2 publication

On October 12, 2026, the owner-approved [PyPI workflow](https://github.com/pubship/pubship/actions/runs/38191643935) and [MCP Registry workflow](https://github.com/pubship/pubship/actions/runs/38192162737) completed successfully for the accepted release commit `383d2f40fd2da79fcab8166e09babf9f8f2ebd7c`.

- [GitHub release](https://github.com/pubship/pubship/releases/tag/v0.24.2): wheel, sdist and SHA256SUMS. Published PyPI file hashes match these accepted assets.
- [PyPI](https://pypi.org/project/pubship/0.24.2/): version 0.24.2, license `AGPL-3.0-only`, Homepage `https://pubship.dev`.
- [Official MCP Registry entry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.pubship%2Fpubship/versions/0.24.2): active and matches the accepted `server.json`.
- `env -u GOOGLE_APPLICATION_CREDENTIALS uvx --refresh pubship@0.24.2 --check` reports version 0.24.2, 172 inventory methods and 170 enabled methods without Google calls.

The official MCP Registry publication does not establish availability in GitHub's curated MCP catalogue or refresh a third-party directory's build. Verify those listings separately.
