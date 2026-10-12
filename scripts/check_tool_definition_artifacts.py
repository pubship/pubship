"""Compare actual MCP discovery from source, the wheel and an sdist-built wheel.

Dependency installation may use the package index. Discovery itself is isolated,
credential-free and network-blocked by export_tool_definitions.py. No Google API
calls are made. Persist only the definition/provenance JSON outside the checkout.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

PROFILES = ("local-default", "local-all-opt-ins", "self-host")


def clean_environment():
    """Exclude operator settings and Python import overrides from every child."""
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("GOOGLE_PLAY_", "PUBSHIP_SERVER_", "PYTHON"))
        and name != "GOOGLE_APPLICATION_CREDENTIALS"
    }


def run(command, *, cwd, environment):
    subprocess.run(command, cwd=cwd, env=environment, check=True, timeout=300)


def compare_definitions(reference, candidate):
    """Compare complete SDK payloads, including descriptions and every annotation."""
    for profile in PROFILES:
        expected = json.loads((reference / f"{profile}.json").read_text())
        actual = json.loads((candidate / f"{profile}.json").read_text())
        if json.dumps(actual, sort_keys=True, allow_nan=False) != json.dumps(
            expected, sort_keys=True, allow_nan=False
        ):
            raise ValueError(f"{candidate.name}/{profile}: client-visible definitions differ")
        print(f"{candidate.name}/{profile}: {len(actual['tools'])} tool definitions match source")
    source_version = json.loads((reference / "provenance.json").read_text())["version"]
    artifact_version = json.loads((candidate / "provenance.json").read_text())["version"]
    if artifact_version != source_version:
        raise ValueError(f"{candidate.name}: installed version differs from source")


def check_artifacts(root, dist, output):
    root, dist, output = root.resolve(), dist.resolve(), output.resolve()
    if output.is_relative_to(root):
        raise ValueError("Keep artifact discovery evidence outside the checkout")
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    wheel = dist / f"pubship-{version}-py3-none-any.whl"
    sdist = dist / f"pubship-{version}.tar.gz"
    if not wheel.is_file() or not sdist.is_file():
        raise ValueError(
            "Build the current version's wheel and sdist before discovery verification"
        )
    # Refuse stale evidence rather than accepting payloads left by an earlier run.
    output.mkdir(parents=True, exist_ok=True)
    for label in ("source", "wheel", "sdist-wheel"):
        (output / label).mkdir()
    environment = clean_environment()
    exporter = root / "scripts/export_tool_definitions.py"
    discovery = root / "scripts/discover_tool_definition_artifact.py"
    with tempfile.TemporaryDirectory(prefix="pubship-artifact-tools-") as temporary:
        work = Path(temporary).resolve()
        requirements = work / "requirements.txt"
        run(
            [
                "uv",
                "export",
                "--quiet",
                "--frozen",
                "--all-extras",
                "--no-default-groups",
                "--group",
                "build",
                "--no-emit-project",
                "--no-annotate",
                "--no-header",
                "--output-file",
                str(requirements),
            ],
            cwd=root,
            environment=environment,
        )
        run(
            [
                sys.executable,
                "-I",
                str(discovery),
                str(root / "src"),
                str(exporter),
                str(output / "source"),
            ],
            cwd=work,
            environment=environment,
        )
        rebuilt = work / "rebuilt"
        for label in ("wheel", "sdist-wheel"):
            venv = work / label
            python = venv / "bin/python"
            run(
                ["uv", "venv", "--python", sys.executable, str(venv)],
                cwd=work,
                environment=environment,
            )
            run(
                [
                    "uv",
                    "pip",
                    "sync",
                    "--python",
                    str(python),
                    "--require-hashes",
                    str(requirements),
                ],
                cwd=work,
                environment=environment,
            )
            artifact = wheel
            if label == "sdist-wheel":
                run(
                    [
                        "uv",
                        "build",
                        "--wheel",
                        "--no-build-isolation",
                        "--python",
                        str(python),
                        "--out-dir",
                        str(rebuilt),
                        str(sdist),
                    ],
                    cwd=work,
                    environment=environment,
                )
                artifact = rebuilt / wheel.name
                if not artifact.is_file():
                    raise ValueError("sdist build did not produce the expected wheel")
            run(
                ["uv", "pip", "install", "--python", str(python), "--no-deps", str(artifact)],
                cwd=work,
                environment=environment,
            )
            run(
                [str(python), "-I", str(discovery), "-", str(exporter), str(output / label)],
                cwd=work,
                environment=environment,
            )
            compare_definitions(output / "source", output / label)
    print("Artifact discovery passed: 3 profiles from source, wheel and sdist-built wheel.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    check_artifacts(Path(__file__).resolve().parents[1], args.dist_dir, args.output_dir)


if __name__ == "__main__":
    main()
