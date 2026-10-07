"""Download and verify accepted GitHub release bytes; never build or upload.

Run from a trusted main checkout with complete Git history. Inputs are environment
variables to avoid shell interpolation. Failure leaves no publishable directory.
"""

import base64
import configparser
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from check_artifacts import inspect_member
from distribution_metadata import SERVER_NAME, metadata

REPOSITORY = "pubship/pubship"
PACKAGE = "pubship"
MAX_ASSET = 32 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024
REQUIRED_JOBS = {
    ".github/workflows/ci.yml": {
        "Python 3.11 / ubuntu-latest",
        "Python 3.14 / ubuntu-latest",
        "Python 3.14 / macos-latest",
        "Hosted consent / Chromium, Firefox, WebKit",
        "Non-root hosted image",
    },
    ".github/workflows/secrets.yml": {"gitleaks"},
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_inputs(tag, commit, checksum):
    require(
        bool(re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag)),
        "Only regular vX.Y.Z release tags are supported.",
    )
    require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "Expected full source commit SHA.")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", checksum)), "Expected SHA256SUMS SHA-256.")


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


class NoAuthenticatedRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the workflow token to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Authenticated GitHub API redirects are not permitted.")


def api(path):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.build_opener(NoAuthenticatedRedirect()).open(
        request, timeout=30
    ) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    require(len(data) <= 8 * 1024 * 1024, "GitHub response too large.")
    return json.loads(data)


def verify_ci(commit):
    # Only exact-source main push runs count. PR merge refs or checks from a fork
    # cannot establish acceptance. The latest run for each workflow must pass.
    for workflow, expected in REQUIRED_JOBS.items():
        name = workflow.rsplit("/", 1)[1]
        runs = api(f"actions/workflows/{name}/runs?head_sha={commit}&event=push&per_page=100")[
            "workflow_runs"
        ]
        runs = [
            r
            for r in runs
            if r["head_sha"] == commit
            and r["head_branch"] == "main"
            and r["head_repository"]["full_name"] == REPOSITORY
            and r["path"] == workflow
        ]
        require(bool(runs), f"No exact-commit main CI run for {workflow}.")
        run = max(runs, key=lambda r: r["id"])
        require(
            run["status"] == "completed" and run["conclusion"] == "success",
            f"Latest {workflow} run is not successful.",
        )
        result = api(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
        jobs = result["jobs"]
        require(result["total_count"] <= 100, "CI job list exceeded verification bound.")
        successful = {j["name"] for j in jobs if j["conclusion"] == "success"}
        require(expected <= successful, f"Required jobs missing or unsuccessful in {workflow}.")


def source_files(commit):
    names = git("ls-tree", "-r", "--name-only", commit).decode().splitlines()
    return {name: git("show", f"{commit}:{name}") for name in names}


def unpack(path, *, sdist_root=None):
    files = {}
    total = 0

    def add(name, data):
        nonlocal total
        require(name not in files, "Duplicate archive member.")
        total += len(data)
        require(total <= MAX_UNPACKED, "Unpacked archive exceeds bound.")
        inspect_member(name, data)
        files[name] = data

    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            require(len(archive.infolist()) <= 10000, "Too many archive entries.")
            for item in archive.infolist():
                require(
                    not item.is_dir() and item.file_size <= MAX_ASSET,
                    "Unexpected or oversized wheel member.",
                )
                require((item.external_attr >> 16) & 0o170000 != 0o120000, "Archive link.")
                add(item.filename, archive.read(item))
    else:
        with tarfile.open(path, "r:gz") as archive:
            for index, item in enumerate(archive):
                require(index < 10000 and item.size <= MAX_ASSET, "Archive exceeds bound.")
                # Directories have no payload, but their paths still affect an
                # installer's extraction. Validate before discarding them.
                require("\\" not in item.name, "Archive path uses unsupported separators.")
                inspect_member(item.name, b"")
                if sdist_root is not None:
                    require(
                        item.name.rstrip("/") == sdist_root.rstrip("/")
                        or item.name.startswith(sdist_root),
                        "Invalid sdist root.",
                    )
                require(item.isfile() or item.isdir(), "Archive special file or link.")
                if item.isfile():
                    add(item.name, archive.extractfile(item).read())
    return files


def normalized_requirement(value):
    # This project uses version specifiers and optional-extra markers only.
    # Fail closed if source adopts a different PEP 508 form until reviewed.
    match = re.fullmatch(
        r"([A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?)([<>=!~0-9.,* ]*)(?:;\s*extra == ['\"]([a-z0-9_-]+)['\"])?",
        value,
    )
    require(match is not None, "Unsupported distribution requirement format.")
    name, versions, extra = match.groups()
    return (
        name.lower().replace("_", "-"),
        tuple(sorted(versions.replace(" ", "").split(","))),
        extra or "",
    )


def verify_metadata(data, project):
    parsed = BytesParser().parsebytes(data)
    require(
        parsed["Name"] == project["name"] and parsed["Version"] == project["version"],
        "Package name/version mismatch.",
    )
    require(
        f"mcp-name: {SERVER_NAME}" in parsed.get_payload(), "Missing Registry ownership marker."
    )

    dependencies = list(project["dependencies"])
    for extra, requirements in project.get("optional-dependencies", {}).items():
        dependencies.extend(f"{value}; extra == '{extra}'" for value in requirements)
    require(
        sorted(map(normalized_requirement, parsed.get_all("Requires-Dist", [])))
        == sorted(map(normalized_requirement, dependencies)),
        "Package dependencies differ from source.",
    )
    require(parsed["Requires-Python"] == project["requires-python"], "Python requirement mismatch.")
    require(
        set(parsed.get_all("Provides-Extra", [])) == set(project.get("optional-dependencies", {})),
        "Optional dependency groups mismatch.",
    )


def verify_archives(wheel, sdist, sources, version):
    project_config = tomllib.loads(sources["pyproject.toml"].decode())
    project = project_config["project"]
    require(
        project["version"] == version and project["name"] == "pubship",
        "Source package version mismatch.",
    )
    require(
        project["scripts"].get("pubship") == "pubship.server:main",
        "Missing Registry-compatible console entry point.",
    )
    require(json.loads(sources["server.json"]) == metadata(project), "Stale Registry metadata.")
    include = project_config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    expected = {
        name: data
        for name, data in sources.items()
        if any(name == p or name.startswith(p + "/") for p in include)
    }
    if ".gitignore" in sources:
        expected[".gitignore"] = sources[".gitignore"]
    prefix = f"{PACKAGE}-{version}/"
    raw = unpack(sdist, sdist_root=prefix)
    require(all(n.startswith(prefix) for n in raw), "Invalid sdist root.")
    contents = {n[len(prefix) :]: data for n, data in raw.items()}
    package_metadata = contents.pop("PKG-INFO")
    verify_metadata(package_metadata, project)
    require(
        package_metadata.partition(b"\n\n")[2] == sources["README.md"],
        "Package description differs from accepted README.",
    )
    require(contents == expected, "Source archive differs from accepted source tree.")
    contents = unpack(wheel)
    info = f"{PACKAGE}-{version}.dist-info/"
    require(contents.get(info + "METADATA") == package_metadata, "Wheel and sdist metadata differ.")
    wheel_metadata = BytesParser().parsebytes(contents.get(info + "WHEEL", b""))
    wheel_versions = wheel_metadata.get_all("Wheel-Version", [])
    require(len(wheel_versions) == 1, "Missing or duplicate Wheel-Version metadata.")
    wheel_version = re.fullmatch(r"([0-9]+)\.([0-9]+)", wheel_versions[0])
    # Wheel installers must reject a newer major format. Minor revisions and
    # generator details may vary without changing the accepted source files.
    require(
        wheel_version is not None and int(wheel_version[1]) == 1,
        "Unsupported Wheel-Version metadata.",
    )
    record = contents.get(info + "RECORD", b"")
    rows = list(csv.reader(io.StringIO(record.decode())))
    require(
        len(rows) == len(contents) and {r[0] for r in rows} == set(contents),
        "Incomplete wheel RECORD.",
    )
    for name, digest, size in rows:
        if name == info + "RECORD":
            require(not digest and not size, "Unexpected RECORD self-digest.")
        else:
            actual = (
                base64.urlsafe_b64encode(hashlib.sha256(contents[name]).digest())
                .rstrip(b"=")
                .decode()
            )
            require(
                digest == f"sha256={actual}" and size == str(len(contents[name])),
                "Wheel RECORD mismatch.",
            )
    expected_wheel = {
        n.removeprefix("src/"): data for n, data in sources.items() if n.startswith("src/pubship/")
    }
    for license_name in project["license-files"]:
        expected_wheel[info + "licenses/" + license_name] = sources[license_name]
    entry_points = contents.pop(info + "entry_points.txt", b"").decode()

    entry_config = configparser.ConfigParser()
    entry_config.read_string(entry_points)
    require(
        dict(entry_config["console_scripts"]) == project["scripts"], "Wheel entry points mismatch."
    )
    for generated in ("METADATA", "WHEEL", "RECORD"):
        contents.pop(info + generated, None)
    require(contents == expected_wheel, "Wheel package differs from accepted source tree.")


def download_asset(asset, tag):
    name = asset["name"]
    require(PurePosixPath(name).name == name, "Invalid asset name.")
    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    require(
        asset["browser_download_url"] == url and 0 < asset["size"] <= MAX_ASSET,
        "Unexpected release asset URL or size.",
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(MAX_ASSET + 1)
    require(len(data) == asset["size"] <= MAX_ASSET, "Asset length mismatch.")
    digest = hashlib.sha256(data).hexdigest()
    require(asset.get("digest") == "sha256:" + digest, "GitHub asset digest mismatch.")
    return data


def verify_checksums(data, expected_digest, assets):
    require(
        hashlib.sha256(data).hexdigest() == expected_digest,
        "Accepted checksum-file digest mismatch.",
    )
    entries = {}
    for line in data.decode("ascii").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
        require(match is not None, "Invalid checksum entry.")
        digest, name = match.groups()
        require(name not in entries, "Duplicate checksum entry.")
        entries[name] = digest
    require(set(entries) == set(assets), "Unexpected checksum file membership.")
    for name, body in assets.items():
        require(hashlib.sha256(body).hexdigest() == entries[name], "Release checksum mismatch.")


def main():
    tag = os.environ.get("RELEASE_TAG", "")
    commit = os.environ.get("ACCEPTED_COMMIT", "")
    checksum = os.environ.get("CHECKSUMS_SHA256", "")
    validate_inputs(tag, commit, checksum)
    require(not Path("publish-dist").exists(), "Refusing existing publication directory.")
    require(
        git("rev-parse", f"refs/tags/{tag}^{{commit}}").decode().strip() == commit,
        "Release tag does not match accepted source.",
    )
    subprocess.run(["git", "merge-base", "--is-ancestor", commit, "origin/main"], check=True)
    verify_ci(commit)
    release = api(f"releases/tags/{tag}")
    require(
        not release["draft"] and not release["prerelease"] and release["tag_name"] == tag,
        "Release is not a published regular release.",
    )
    version = tag[1:]
    names = [f"{PACKAGE}-{version}-py3-none-any.whl", f"{PACKAGE}-{version}.tar.gz"]
    indexed = {a["name"]: a for a in release["assets"]}
    require(
        set(indexed) == {*names, "SHA256SUMS"} and len(release["assets"]) == 3,
        "Expected exactly wheel, source archive and SHA256SUMS.",
    )
    bodies = {name: download_asset(indexed[name], tag) for name in names}
    verify_checksums(download_asset(indexed["SHA256SUMS"], tag), checksum, bodies)
    sources = source_files(commit)
    with tempfile.TemporaryDirectory(prefix="pubship-release-") as temporary:
        directory = Path(temporary)
        for name, body in bodies.items():
            (directory / name).write_bytes(body)
        verify_archives(directory / names[0], directory / names[1], sources, version)
        # Existing versions fail closed: never overwrite, skip or mix artifact sets.
        try:
            urllib.request.urlopen(
                f"https://pypi.org/pypi/pubship/{version}/json", timeout=30
            ).close()
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            raise ValueError("PyPI version already exists; refusing publication.")
        Path("publish-dist").mkdir(mode=0o700)
        for name, body in bodies.items():
            Path("publish-dist", name).write_bytes(body)
    print(f"Verified {tag} at {commit}: exact source, required CI and accepted artifact digests.")


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
        # No headers, tokens, package contents or response bodies in diagnostics.
        raise SystemExit(f"Publication preparation failed ({type(error).__name__}).") from None
