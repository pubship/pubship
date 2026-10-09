"""Export actual MCP tools/list under synthetic, credential-free configurations.

Run with the hosted extra installed. The three profiles cover local defaults,
all local opt-ins, and the fixed self-hosted tool surface. Provider access and
network connections are prohibited while collecting definitions.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from mcp import Client

from pubship.account_access_config import METHODS as ACCOUNT_METHODS
from pubship.appstore_contracts import APPSTORE_METHODS
from pubship.config import Settings
from pubship.coverage import (
    ARTIFACT_METHODS,
    EDIT_WRITE_METHODS,
    EXTERNAL_TRANSACTION_METHODS,
    MONETIZATION_WRITE_METHODS,
    PRICE_MIGRATION_METHODS,
    PURCHASE_ACTION_METHODS,
    SHARING_METHODS,
    SUPPORT_METHODS,
)
from pubship.hosted import create_hosted_app
from pubship.hosted_config import HostedSettings
from pubship.server import create_server
from pubship.signing import METHODS as SIGNING_METHODS


async def _list_tools(server):
    async with Client(server) as client:
        result = await client.list_tools()
        return result.model_dump(by_alias=True, exclude_none=True)


async def export_definitions() -> dict[str, dict]:
    """Return client-visible definitions without inspecting operator configuration."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GOOGLE_PLAY_", "PUBSHIP_SERVER_"))
        and key != "GOOGLE_APPLICATION_CREDENTIALS"
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        patch(
            "pubship.auth.Auth.token", side_effect=AssertionError("Credential access prohibited")
        ),
        patch("socket.socket.connect", side_effect=AssertionError("Network access prohibited")),
        patch("socket.socket.connect_ex", side_effect=AssertionError("Network access prohibited")),
        patch("socket.getaddrinfo", side_effect=AssertionError("Network access prohibited")),
    ):
        profiles = {"local-default": await _list_tools(create_server(Settings()))}
        packages = frozenset({"com.example.app"})
        settings = Settings(
            packages=packages,
            write_packages=packages,
            edit_packages=packages,
            track_packages=packages,
            sensitive_read_packages=packages,
            edit_write_packages=packages,
            edit_write_methods=EDIT_WRITE_METHODS,
            artifact_packages=packages,
            artifact_methods=ARTIFACT_METHODS | SHARING_METHODS,
            support_packages=packages,
            support_methods=SUPPORT_METHODS,
            purchase_packages=packages,
            purchase_methods=PURCHASE_ACTION_METHODS,
            external_transaction_packages=packages,
            external_transaction_methods=EXTERNAL_TRANSACTION_METHODS,
            price_migration_packages=packages,
            price_migration_methods=PRICE_MIGRATION_METHODS,
            monetization_packages=packages,
            monetization_methods=MONETIZATION_WRITE_METHODS,
        )
        grants = {
            "GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPERS": "12345",
            "GOOGLE_PLAY_ACCOUNT_ACCESS_METHODS": ",".join(sorted(ACCOUNT_METHODS)),
            "GOOGLE_PLAY_SIGNING_APPS": "com.example.app",
            "GOOGLE_PLAY_SIGNING_METHODS": ",".join(sorted(SIGNING_METHODS)),
            "GOOGLE_PLAY_SIGNING_KMS_VERSIONS": (
                "projects/example-project/locations/global/keyRings/ring/"
                "cryptoKeys/key/cryptoKeyVersions/1"
            ),
            "GOOGLE_PLAY_APPSTORE_STORES": "com.example.app",
            "GOOGLE_PLAY_APPSTORE_PACKAGES": "com.example.app",
            "GOOGLE_PLAY_APPSTORE_METHODS": ",".join(sorted(APPSTORE_METHODS)),
        }
        with patch.dict(os.environ, grants):
            profiles["local-all-opt-ins"] = await _list_tools(create_server(settings))

        with tempfile.TemporaryDirectory(prefix="pubship-tool-definitions-") as directory:
            captured = []

            def capture_server(**kwargs):
                server = create_server(**kwargs)
                captured.append(server)
                return server

            hosted_settings = HostedSettings(
                "https://mcp.example.test",
                "synthetic-client",
                "synthetic-secret",
                Path(directory) / "vault.db",
                Fernet.generate_key(),
                allowed_emails=frozenset({"owner@example.test"}),
            )
            with patch("pubship.hosted.create_server", side_effect=capture_server):
                app = create_hosted_app(hosted_settings)
            try:
                profiles["self-host"] = await _list_tools(captured[0])
            finally:
                app.state.hosted_registry.close()
                app.state.hosted_transfers.close()
                app.state.vault.close()
        return profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--lint",
        action="store_true",
        help="Write unchanged offline TDQS JSON reports (requires the pinned tdqs dependency group).",
    )
    args = parser.parse_args()
    profiles = asyncio.run(export_definitions())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in profiles.items():
        path = args.output_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{name}: {len(payload['tools'])} tools -> {path}", flush=True)
        if args.lint:
            # TDQS lint is the upstream deterministic, key-free path. Keep every
            # diagnostic in the report; reviewed acceptance is a separate gate.
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tdqs",
                    "lint",
                    "--file",
                    str(path),
                    "--format",
                    "json",
                    "--fail-on",
                    "never",
                    "--output",
                    str(args.output_dir / f"{name}-lint.json"),
                ],
                check=True,
            )


if __name__ == "__main__":
    main()
