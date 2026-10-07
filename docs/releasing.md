# Bundled releases

Group completed issue-linked changes into a reviewed release. Do not publish a version for each API family. Use semantic versions and preserve existing tags and artifacts.

1. Freeze the candidate commit and update version metadata, CHANGELOG, release notes, README and website claims.
2. Run the complete checks in README with GOOGLE_APPLICATION_CREDENTIALS unset. Use synthetic providers. Review architecture, engineering, QA and delivery evidence, including all skips and limits.
3. Build the wheel and source archive with `uv build`. Inspect with `scripts/check_artifacts.py` and Gitleaks. Create SHA256SUMS for the exact release assets.
4. With owner authorization, tag the accepted commit, create a GitHub release using `docs/releases/vX.Y.Z.md`, and attach the wheel, source archive and SHA256SUMS. Preserve those bytes.
5. Configure the protected environments described in [distribution](distribution.md). Dispatch `publish-pypi.yml` with tag, accepted full commit and independently recorded checksum-manifest hash. The owner approves the deployment in GitHub.
6. After PyPI verification, dispatch `publish-mcp-registry.yml` for the same version and accepted metadata. The owner approves it. Verify the public registry and credential-free installed command.

For the first PubShip release use 0.24.0. Local migration, source publication, archiving predecessor repositories and package publication have separate owner authorization gates. Do not skip them.
