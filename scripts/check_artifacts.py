"""Reject private files and credential markers from distribution artifacts.

This is a narrow packaging guard, not a replacement for Gitleaks or human review.
Do not print contents of a file that fails the check.
"""

import argparse
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

BAD_NAMES = re.compile(
    r"(^|/)(\.env(?:\..*)?|client_secret[^/]*|application_default_credentials\.json|[^/]*credentials[^/]*\.json|[^/]*service-account[^/]*\.json|secrets|private)(/|$)|\.(pem|key|p12|pfx|sqlite[\w-]*|db)$",
    re.I,
)
MARKERS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb'"type"\s*:\s*"service_account"'),
    re.compile(rb'"(?:refresh_token|client_secret)"\s*:\s*"[^"\s]{16,}"'),
)


def inspect_member(name, data):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or BAD_NAMES.search(name):
        raise ValueError("Distribution includes a forbidden private or unsafe path.")
    if any(pattern.search(data) for pattern in MARKERS):
        raise ValueError("Distribution contains a possible credential marker; inspect privately.")


def check(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                inspect_member(name, archive.read(name))
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path) as archive:
            for member in archive:
                if member.issym() or member.islnk():
                    raise ValueError(
                        "Distribution contains a link; package regular source files only."
                    )
                file = archive.extractfile(member) if member.isfile() else None
                inspect_member(member.name, file.read() if file else b"")
    else:
        raise ValueError("Unknown distribution format.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path("dist"))
    args = parser.parse_args()
    paths = sorted([*args.directory.glob("*.whl"), *args.directory.glob("*.tar.gz")])
    if len(paths) < 2:
        raise SystemExit("Build both wheel and source archive before checking artifacts.")
    try:
        for path in paths:
            check(path)
    except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise SystemExit(str(error)) from None
    print(f"Checked {len(paths)} distribution artifacts; no forbidden files or credential markers.")


if __name__ == "__main__":
    main()
