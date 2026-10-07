"""Synchronize the local-only MCP Registry candidate with project metadata."""

import argparse
import json
import tomllib
from pathlib import Path

SERVER_NAME = "io.github.pubship/pubship"


def metadata(project):
    return {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": SERVER_NAME,
        "title": "PubShip",
        "description": "Google Play reads and explicitly opted-in operations using your own local Google access.",
        "version": project["version"],
        "repository": {"url": "https://github.com/pubship/pubship", "source": "github"},
        "websiteUrl": "https://pubship.dev",
        "packages": [
            {
                "registryType": "pypi",
                "registryBaseUrl": "https://pypi.org",
                "identifier": project["name"],
                "version": project["version"],
                "runtimeHint": "uvx",
                "transport": {"type": "stdio"},
            }
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    content = json.dumps(metadata(project), indent=2) + "\n"
    path = Path("server.json")
    if args.check:
        if not path.exists() or path.read_text() != content:
            raise SystemExit("server.json is stale; run scripts/distribution_metadata.py")
    else:
        path.write_text(content)


if __name__ == "__main__":
    main()
