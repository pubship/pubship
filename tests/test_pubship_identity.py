import json
import subprocess
import sys
import tomllib
from pathlib import Path

from pubship import __version__
from pubship.server import create_server

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_identity_and_command_surface():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "pubship"
    assert project["version"] == __version__ == "0.24.0"
    assert project["license"] == "AGPL-3.0-only"
    assert project["scripts"] == {
        "pubship": "pubship.server:main",
        "pubship-server": "pubship.hosted:main",
    }
    assert create_server().name == "pubship"


def test_credential_free_check_is_bounded_and_does_not_start_stdio():
    result = subprocess.run(
        [sys.executable, "-m", "pubship.server", "--check"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert json.loads(result.stdout) == {
        "name": "pubship",
        "version": "0.24.0",
        "methods": 172,
        "enabled": 170,
    }
