"""Publication rejects altered source, metadata, checksums and weak CI evidence."""

import base64
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "prepare_pypi_release", SCRIPTS / "prepare_pypi_release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
sys.path.pop(0)


@pytest.fixture
def package(tmp_path):
    config = b"""[project]
name = "pubship"
version = "1.2.3"
requires-python = ">=3.11"
dependencies = ["mcp==2.3.0"]
license-files = ["LICENSE"]
[project.scripts]
pubship = "pubship.server:main"
[tool.hatch.build.targets.sdist]
include = ["pyproject.toml", "README.md", "LICENSE", "src"]
"""
    readme = f"# PubShip\n<!-- mcp-name: {release.SERVER_NAME} -->\n".encode()
    metadata = (
        b"Name: pubship\nVersion: 1.2.3\nRequires-Python: >=3.11\nRequires-Dist: mcp==2.3.0\n\n"
        + readme
    )
    sources = {
        "pyproject.toml": config,
        "README.md": readme,
        "LICENSE": b"AGPL-3.0-only",
        "src/pubship/server.py": b"def main(): pass\n",
    }
    project = release.tomllib.loads(config.decode())["project"]
    sources["server.json"] = json.dumps(release.metadata(project)).encode()
    root = "pubship-1.2.3/"
    info = "pubship-1.2.3.dist-info/"
    wheel_files = {
        "pubship/server.py": sources["src/pubship/server.py"],
        info + "METADATA": metadata,
        info + "WHEEL": b"Wheel-Version: 1.0\n",
        info + "licenses/LICENSE": b"AGPL-3.0-only",
        info + "entry_points.txt": b"[console_scripts]\npubship = pubship.server:main\n",
    }
    sdist_files = {root + k: v for k, v in sources.items() if k != "server.json"}
    sdist_files[root + "PKG-INFO"] = metadata

    def write(wheel_changes=None, sdist_changes=None, *, sdist_directories=(), omit_wheel=False):
        whl = wheel_files | (wheel_changes or {})
        if omit_wheel:
            del whl[info + "WHEEL"]
        record = ""
        for name, data in whl.items():
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record += f"{name},sha256={digest},{len(data)}\n"
        whl[info + "RECORD"] = (record + f"{info}RECORD,,\n").encode()
        wheel = tmp_path / "package.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, data in whl.items():
                archive.writestr(name, data)
        sdist = tmp_path / "package.tar.gz"
        with tarfile.open(sdist, "w:gz") as archive:
            for name in sdist_directories:
                item = tarfile.TarInfo(name)
                item.type = tarfile.DIRTYPE
                archive.addfile(item)
            for name, data in (sdist_files | (sdist_changes or {})).items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        return wheel, sdist

    return sources, info, root, write


def test_accepted_bytes(package):
    sources, _, _, write = package
    release.verify_archives(*write(), sources, "1.2.3")


@pytest.mark.parametrize(
    "directory",
    [
        "../../unexpected",
        "/absolute",
        "another-project",
        "pubship-1.2.3/../escape",
        "pubship-1.2.3/..\\escape",
    ],
)
def test_sdist_directory_paths_are_checked_without_extraction(package, directory):
    sources, _, _, write = package
    with pytest.raises(ValueError):
        release.verify_archives(*write(sdist_directories=[directory]), sources, "1.2.3")


def test_sdist_accepts_safe_root_and_child_directory_entries(package):
    sources, _, root, write = package
    release.verify_archives(
        *write(sdist_directories=[root, root + "src", root + "src/pubship"]),
        sources,
        "1.2.3",
    )


@pytest.mark.parametrize(
    "wheel_metadata",
    [
        None,
        b"Generator: synthetic\n",
        b"Wheel-Version: 999.0\n",
        b"Wheel-Version: 0.1\n",
        b"Wheel-Version: invalid\n",
        b"Wheel-Version: 1.0\nWheel-Version: 999.0\n",
    ],
)
def test_wheel_requires_supported_format_metadata(package, wheel_metadata):
    sources, info, _, write = package
    changes = {info + "WHEEL": wheel_metadata} if wheel_metadata is not None else None
    with pytest.raises(ValueError, match="Wheel-Version"):
        release.verify_archives(
            *write(changes, omit_wheel=wheel_metadata is None), sources, "1.2.3"
        )


def test_wheel_minor_and_generator_variation_preserves_source_parity(package):
    sources, info, _, write = package
    release.verify_archives(
        *write({info + "WHEEL": b"Wheel-Version: 1.1\nGenerator: synthetic reviewed build\n"}),
        sources,
        "1.2.3",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "wheel_source",
        "sdist_source",
        "extra",
        "traversal",
        "metadata",
        "entrypoint",
        "marker",
        "version",
        "manifest",
        "dependencies",
    ],
)
def test_reject_altered_artifacts(package, mutation):
    sources, info, root, write = package
    wheel, sdist = {}, {}
    if mutation == "wheel_source":
        wheel["pubship/server.py"] = b"malicious()"
    elif mutation == "sdist_source":
        sdist[root + "src/pubship/server.py"] = b"malicious()"
    elif mutation == "extra":
        wheel["evil.pth"] = b"import evil"
    elif mutation == "traversal":
        sdist[root + "../escape"] = b"escape"
    elif mutation == "metadata":
        wheel[info + "METADATA"] = b"Name: other\nVersion: 1.2.3\n"
    elif mutation == "entrypoint":
        wheel[info + "entry_points.txt"] = b"[console_scripts]\npubship=evil:main\n"
    elif mutation == "marker":
        sources["README.md"] = b"Missing ownership"
    elif mutation == "version":
        sources["pyproject.toml"] = sources["pyproject.toml"].replace(b"1.2.3", b"1.2.4")
    elif mutation == "manifest":
        sources["server.json"] = b"{}"
    elif mutation == "dependencies":
        sources["pyproject.toml"] = sources["pyproject.toml"].replace(b"mcp==2.3.0", b"evil==1.0")
    with pytest.raises(ValueError):
        release.verify_archives(*write(wheel, sdist), sources, "1.2.3")


@pytest.mark.parametrize(
    "tag", ["v1.2.3;touch /tmp/oops", "$(id)", "--help", "v1.2.3-rc.1", "v01.2.3"]
)
def test_tag_input_is_not_shell_code(tag):
    with pytest.raises(ValueError):
        release.validate_inputs(tag, "a" * 40, "b" * 64)


def test_checksums_require_exact_accepted_set():
    body = b"wheel"
    data = f"{hashlib.sha256(body).hexdigest()}  package.whl\n".encode()
    accepted = hashlib.sha256(data).hexdigest()
    release.verify_checksums(data, accepted, {"package.whl": body})
    for digest, assets in [
        ("0" * 64, {"package.whl": body}),
        (accepted, {"package.whl": b"changed"}),
        (accepted, {"other.whl": body}),
    ]:
        with pytest.raises(ValueError):
            release.verify_checksums(data, digest, assets)


@pytest.mark.parametrize(
    "failure", [None, "wrong_commit", "fork", "pr", "failed", "skipped_job", "latest_failed"]
)
def test_ci_requires_exact_successful_main_jobs(monkeypatch, failure):
    commit = "a" * 40

    def api(path):
        if "/runs?" in path:
            name = path.split("/")[2]
            run = {
                "id": 1,
                "head_sha": commit,
                "head_branch": "main",
                "head_repository": {"full_name": release.REPOSITORY},
                "path": ".github/workflows/" + name,
                "status": "completed",
                "conclusion": "success",
            }
            if failure == "wrong_commit":
                run["head_sha"] = "b" * 40
            elif failure == "fork":
                run["head_repository"] = {"full_name": "attacker/fork"}
            elif failure == "pr":
                run["head_branch"] = "feature"
            elif failure == "failed":
                run["conclusion"] = "failure"
            runs = [run]
            if failure == "latest_failed":
                runs.append(run | {"id": 2, "conclusion": "failure"})
            return {"workflow_runs": runs}
        jobs = [
            {"name": name, "conclusion": "success"}
            for names in release.REQUIRED_JOBS.values()
            for name in names
        ]
        if failure == "skipped_job":
            jobs[0]["conclusion"] = "skipped"
        return {"total_count": len(jobs), "jobs": jobs}

    monkeypatch.setattr(release, "api", api)
    if failure:
        with pytest.raises(ValueError):
            release.verify_ci(commit)
    else:
        release.verify_ci(commit)


def test_metadata_tracks_version_and_never_lists_remote():
    root = SCRIPTS.parent
    project = release.tomllib.loads((root / "pyproject.toml").read_text())["project"]
    manifest = json.loads((root / "server.json").read_text())
    assert manifest == release.metadata(project)
    assert "remotes" not in manifest
    assert project["scripts"][project["name"]] == "pubship.server:main"


@pytest.mark.parametrize(
    "destination", ["https://attacker.invalid/", "https://api.github.com/moved"]
)
def test_authenticated_api_redirects_fail_closed(destination):
    request = release.urllib.request.Request(
        "https://api.github.com/repos/pubship/pubship/releases",
        headers={"Authorization": "Bearer synthetic-test-token"},
    )
    with pytest.raises(ValueError, match="redirects are not permitted"):
        release.NoAuthenticatedRedirect().redirect_request(
            request, None, 302, "Found", {}, destination
        )
