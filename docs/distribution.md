# Distribution

Package: `pubship`. Registry namespace: `io.github.pubship/pubship`. Release versions are declared in `pyproject.toml` and `server.json`; verify the published version independently. Repository: `pubship/pubship`. License: AGPL-3.0-only.

Source preparation is not publication. A maintainer must create the release and approve the protected publishing environments. Never approve your own deployment or review. PyPI and registry publication must use the same accepted release bytes.

## PyPI pending publisher

- Owner: `pubship`
- Repository: `pubship`
- Workflow: `publish-pypi.yml`
- Environment: `pypi`

The workflow validates the accepted commit, CI and release checksums before Trusted Publishing. Configure a human required reviewer and main-only deployments. No package token belongs in source.

## MCP Registry

Use `publish-mcp-registry.yml` with the `mcp-registry` environment after PyPI succeeds. The manifest namespace is `io.github.pubship/pubship`, using GitHub OIDC. Check the active registry entry and published package version afterward. No submission is implied by repository metadata.
