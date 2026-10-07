"""Verify accepted local release and public PyPI bytes before official publication.

No authentication exchange or registry mutation happens here. The workflow uses
mcp-publisher for those operations; --verify-published only reads public metadata.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import prepare_pypi_release as release
from distribution_metadata import SERVER_NAME, metadata

REGISTRY = "https://registry.modelcontextprotocol.io"
OUTPUT = Path("registry-release")


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Public downloads carry no credentials. Still forbid a redirect to a
        # different host, plain HTTP, URL credentials or an unexpected port.
        validate_public_url(newurl, urllib.parse.urlsplit(req.full_url).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_public_url(url, host):
    parsed = urllib.parse.urlsplit(url)
    release.require(
        parsed.scheme == "https"
        and parsed.hostname == host
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment,
        "Unexpected public download URL.",
    )


def public_bytes(url, host, maximum=release.MAX_ASSET):
    validate_public_url(url, host)
    with urllib.request.build_opener(RestrictedRedirect()).open(url, timeout=60) as response:
        body = response.read(maximum + 1)
    release.require(len(body) <= maximum, "Public response exceeds size bound.")
    return body


def verify_pypi(document, bodies, sources, version, download=public_bytes):
    info = document["info"]
    release.require(
        info["name"] == "pubship" and info["version"] == version,
        "PyPI project/version mismatch.",
    )
    release.require(
        info["description"] == sources["README.md"].decode(),
        "PyPI description differs from accepted README.",
    )
    release.require(
        f"mcp-name: {SERVER_NAME}" in info["description"], "PyPI ownership marker missing."
    )
    files = document["urls"]
    indexed = {item["filename"]: item for item in files}
    release.require(
        len(files) == len(indexed) == len(bodies) and set(indexed) == set(bodies),
        "PyPI file inventory differs from accepted release.",
    )
    for name, accepted in bodies.items():
        item = indexed[name]
        digest = hashlib.sha256(accepted).hexdigest()
        release.require(
            not item["yanked"]
            and item["digests"]["sha256"] == digest
            and item["size"] == len(accepted),
            "PyPI file is yanked or changed.",
        )
        validate_public_url(item["url"], "files.pythonhosted.org")
        release.require(
            urllib.parse.unquote(urllib.parse.urlsplit(item["url"]).path).endswith("/" + name),
            "PyPI file URL does not match its filename.",
        )
        downloaded = download(item["url"], "files.pythonhosted.org")
        release.require(downloaded == accepted, "Public PyPI bytes differ from accepted release.")


def verify_local_manifest(sources, version):
    project = tomllib.loads(sources["pyproject.toml"].decode())["project"]
    manifest = json.loads(sources["server.json"])
    release.require(
        project["version"] == version and manifest == metadata(project),
        "Registry manifest differs from accepted local-only metadata.",
    )
    # Exact canonical equality rejects remotes, credentials, user-supplied
    # arguments, alternate registry URLs and any unreviewed extension fields.
    release.require("remotes" not in manifest, "Hosted Registry entries remain disabled.")
    return manifest


def prepare(tag, commit, checksum, output=OUTPUT):
    release.validate_inputs(tag, commit, checksum)
    release.require(not output.exists(), "Refusing existing Registry preparation directory.")
    release.require(
        release.git("rev-parse", f"refs/tags/{tag}^{{commit}}").decode().strip() == commit,
        "Release tag does not match accepted source.",
    )
    subprocess.run(["git", "merge-base", "--is-ancestor", commit, "origin/main"], check=True)
    release.verify_ci(commit)
    source = release.source_files(commit)
    version = tag[1:]
    verify_local_manifest(source, version)
    published = release.api(f"releases/tags/{tag}")
    release.require(
        not published["draft"] and not published["prerelease"] and published["tag_name"] == tag,
        "Expected a published regular release.",
    )
    names = [f"{release.PACKAGE}-{version}-py3-none-any.whl", f"{release.PACKAGE}-{version}.tar.gz"]
    assets = {item["name"]: item for item in published["assets"]}
    release.require(
        len(published["assets"]) == 3 and set(assets) == {*names, "SHA256SUMS"},
        "Unexpected accepted release assets.",
    )
    bodies = {name: release.download_asset(assets[name], tag) for name in names}
    release.verify_checksums(release.download_asset(assets["SHA256SUMS"], tag), checksum, bodies)
    with tempfile.TemporaryDirectory(prefix="pubship-registry-") as temporary:
        directory = Path(temporary)
        for name, body in bodies.items():
            (directory / name).write_bytes(body)
        release.verify_archives(directory / names[0], directory / names[1], source, version)
    document = json.loads(
        public_bytes(
            f"https://pypi.org/pypi/pubship/{version}/json",
            "pypi.org",
            8 * 1024**2,
        )
    )
    verify_pypi(document, bodies, source, version)
    manifest = verify_local_manifest(source, version)
    require_unpublished(manifest)
    # Write only after all checks pass. The publisher receives the exact accepted
    # source manifest, never generated replacement release metadata.
    output.mkdir(mode=0o700)
    (output / "server.json").write_bytes(source["server.json"])
    print(f"Verified local Registry candidate {SERVER_NAME}@{version} from {commit}.")


def version_url(manifest):
    name = urllib.parse.quote(manifest["name"], safe="")
    version = urllib.parse.quote(manifest["version"], safe="")
    return f"{REGISTRY}/v0.1/servers/{name}/versions/{version}"


def require_unpublished(manifest, fetch=public_bytes):
    try:
        fetch(version_url(manifest), "registry.modelcontextprotocol.io", 1024**2)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise
    raise ValueError("Registry version already exists; inspect it instead of overwriting.")


def verify_published(manifest, fetch=public_bytes):
    document = json.loads(
        fetch(
            version_url(manifest),
            "registry.modelcontextprotocol.io",
            1024**2,
        )
    )
    release.require(
        document["_meta"]["io.modelcontextprotocol.registry/official"]["status"] == "active",
        "Published Registry entry is not active.",
    )
    actual = document["server"]
    # Registry may omit the schema URI and attach its own _meta envelope.
    for field, expected in manifest.items():
        if field != "$schema":
            release.require(actual.get(field) == expected, "Published Registry metadata mismatch.")
    release.require(not actual.get("remotes"), "Unexpected published hosted endpoint.")
    print(f"Public Registry read-back matches {manifest['name']}@{manifest['version']}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-published", action="store_true")
    args = parser.parse_args()
    if args.verify_published:
        verify_published(json.loads((OUTPUT / "server.json").read_bytes()))
    else:
        prepare(
            os.environ.get("RELEASE_TAG", ""),
            os.environ.get("ACCEPTED_COMMIT", ""),
            os.environ.get("CHECKSUMS_SHA256", ""),
        )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as error:
        raise SystemExit(f"Registry preparation failed ({type(error).__name__}).") from None
