"""Registry publication requires accepted bytes and a credential-free local entry."""

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "prepare_registry_release", SCRIPTS / "prepare_registry_release.py"
)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)
sys.path.pop(0)


@pytest.fixture
def published():
    sources = {
        "README.md": f"# Example\n<!-- mcp-name: {registry.SERVER_NAME} -->\n".encode(),
        "pyproject.toml": b'[project]\nname="pubship"\nversion="1.2.3"\n',
    }
    manifest = registry.metadata({"name": "pubship", "version": "1.2.3"})
    sources["server.json"] = json.dumps(manifest).encode()
    bodies = {"package.whl": b"accepted wheel", "package.tar.gz": b"accepted source"}
    document = {
        "info": {
            "name": "pubship",
            "version": "1.2.3",
            "description": sources["README.md"].decode(),
        },
        "urls": [
            {
                "filename": name,
                "size": len(body),
                "yanked": False,
                "digests": {"sha256": hashlib.sha256(body).hexdigest()},
                "url": "https://files.pythonhosted.org/packages/example/" + name,
            }
            for name, body in bodies.items()
        ],
    }
    return sources, manifest, bodies, document


def test_public_pypi_exact_bytes_and_manifest(published):
    sources, manifest, bodies, document = published
    registry.verify_pypi(
        document, bodies, sources, "1.2.3", download=lambda url, host: bodies[url.rsplit("/", 1)[1]]
    )
    assert registry.verify_local_manifest(sources, "1.2.3") == manifest


@pytest.mark.parametrize(
    "mutation",
    [
        "project",
        "version",
        "ownership",
        "extra_file",
        "missing_file",
        "duplicate",
        "digest",
        "yanked",
        "size",
        "download",
        "http",
        "host",
        "credentials",
        "port",
        "filename",
    ],
)
def test_pypi_rejects_mismatched_or_unsafe_publication(published, mutation):
    sources, _, bodies, document = published
    if mutation == "project":
        document["info"]["name"] = "another-project"
    elif mutation == "version":
        document["info"]["version"] = "1.2.4"
    elif mutation == "ownership":
        document["info"]["description"] = "Missing ownership marker"
    elif mutation == "extra_file":
        document["urls"].append(document["urls"][0] | {"filename": "extra.whl"})
    elif mutation == "missing_file":
        document["urls"].pop()
    elif mutation == "duplicate":
        document["urls"].append(document["urls"][0])
    elif mutation == "digest":
        document["urls"][0]["digests"]["sha256"] = "0" * 64
    elif mutation == "yanked":
        document["urls"][0]["yanked"] = True
    elif mutation == "size":
        document["urls"][0]["size"] += 1
    elif mutation in {"http", "host", "credentials", "port", "filename"}:
        document["urls"][0]["url"] = {
            "http": "http://files.pythonhosted.org/package.whl",
            "host": "https://attacker.test/package.whl",
            "credentials": "https://user:secret@files.pythonhosted.org/package.whl",
            "port": "https://files.pythonhosted.org:8443/package.whl",
            "filename": "https://files.pythonhosted.org/other.whl",
        }[mutation]

    def download(url, host):
        return b"different bytes" if mutation == "download" else bodies[url.rsplit("/", 1)[1]]

    with pytest.raises(ValueError):
        registry.verify_pypi(document, bodies, sources, "1.2.3", download=download)


@pytest.mark.parametrize("mutation", ["remote", "secret", "arguments", "version", "package"])
def test_manifest_rejects_hosted_or_unreviewed_fields(published, mutation):
    sources, manifest, _, _ = published
    if mutation == "remote":
        manifest["remotes"] = [{"type": "streamable-http", "url": "https://mcp.example.test/mcp"}]
    elif mutation == "secret":
        manifest["packages"][0]["environmentVariables"] = [{"name": "TOKEN", "value": "credential"}]
    elif mutation == "arguments":
        manifest["packages"][0]["runtimeArguments"] = [
            {"type": "positional", "value": "unreviewed"}
        ]
    elif mutation == "version":
        manifest["version"] = "1.2.4"
    else:
        manifest["packages"][0]["identifier"] = "other"
    sources["server.json"] = json.dumps(manifest).encode()
    with pytest.raises(ValueError):
        registry.verify_local_manifest(sources, "1.2.3")


@pytest.mark.parametrize("mutation", [None, "version", "package", "remote", "inactive"])
def test_registry_readback_matches_accepted_entry(published, mutation):
    _, manifest, _, _ = published
    actual = copy.deepcopy(manifest)
    actual.pop("$schema")
    if mutation == "version":
        actual["version"] = "1.2.4"
    elif mutation == "package":
        actual["packages"][0]["version"] = "1.2.4"
    elif mutation == "remote":
        actual["remotes"] = [{"url": "https://mcp.example.test/mcp"}]

    def fetch(url, host, maximum):
        assert url.endswith("/io.github.pubship%2Fpubship/versions/1.2.3")
        assert host == "registry.modelcontextprotocol.io"
        return json.dumps(
            {
                "server": actual,
                "_meta": {
                    "io.modelcontextprotocol.registry/official": {
                        "status": "deleted" if mutation == "inactive" else "active"
                    }
                },
            }
        ).encode()

    if mutation:
        with pytest.raises(ValueError):
            registry.verify_published(manifest, fetch=fetch)
    else:
        registry.verify_published(manifest, fetch=fetch)


def test_invalid_tag_stops_before_network_or_output(tmp_path, monkeypatch):
    monkeypatch.setattr(registry.release, "api", lambda _: pytest.fail("Network attempted"))
    output = tmp_path / "prepared"
    with pytest.raises(ValueError):
        registry.prepare("v1.2.3; echo injected", "a" * 40, "b" * 64, output)
    assert not output.exists()


def test_py_pi_failure_cannot_leave_publishable_manifest(published, tmp_path, monkeypatch):
    sources, _, _, _ = published
    monkeypatch.setattr(registry.release, "git", lambda *args: b"a" * 40)
    monkeypatch.setattr(registry.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(registry.release, "verify_ci", lambda commit: None)
    monkeypatch.setattr(registry.release, "source_files", lambda commit: sources)
    names = [
        "pubship-1.2.3-py3-none-any.whl",
        "pubship-1.2.3.tar.gz",
        "SHA256SUMS",
    ]
    monkeypatch.setattr(
        registry.release,
        "api",
        lambda path: {
            "tag_name": "v1.2.3",
            "draft": False,
            "prerelease": False,
            "assets": [{"name": n} for n in names],
        },
    )
    monkeypatch.setattr(registry.release, "download_asset", lambda *args: b"fixture")
    monkeypatch.setattr(registry.release, "verify_checksums", lambda *args: None)
    monkeypatch.setattr(registry.release, "verify_archives", lambda *args: None)

    def unavailable(*args):
        raise OSError("Public PyPI is unavailable")

    monkeypatch.setattr(registry, "public_bytes", unavailable)
    output = tmp_path / "prepared"
    with pytest.raises(OSError):
        registry.prepare("v1.2.3", "a" * 40, "b" * 64, output)
    assert not output.exists()


def test_public_redirect_never_changes_host():
    request = registry.urllib.request.Request("https://files.pythonhosted.org/package.whl")
    with pytest.raises(ValueError):
        registry.RestrictedRedirect().redirect_request(
            request, None, 302, "", {}, "https://internal.test/file"
        )


@pytest.mark.parametrize("status", [200, 404, 403, 500])
def test_existing_versions_and_failed_preflight_are_not_overwritten(published, status):
    _, manifest, _, _ = published

    def fetch(url, host, maximum):
        if status != 200:
            raise registry.urllib.error.HTTPError(url, status, "synthetic", {}, None)
        return b"existing public version"

    if status == 404:
        registry.require_unpublished(manifest, fetch)
    else:
        with pytest.raises((ValueError, registry.urllib.error.HTTPError)):
            registry.require_unpublished(manifest, fetch)
