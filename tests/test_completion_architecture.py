"""Independent final architecture regressions; synthetic certificates and no provider writes."""

import asyncio
import base64
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mcp import Client

from pubship import signing
from pubship.config import Settings
from pubship.errors import PlayError
from pubship.server import create_server

APP = "com.example.architecture"
KMS = "projects/example/locations/global/keyRings/testing/cryptoKeys/signing/cryptoKeyVersions/7"
ROTATE = signing.PREFIX + "rotateAppSigningKey"
PRIVILEGED_TOOLS = {
    "read_account_access",
    "prepare_account_access",
    "apply_account_access",
    "prepare_signing_operation",
    "apply_signing_operation",
    "query_appstore",
    "prepare_appstore_operation",
    "apply_appstore_operation",
}


@pytest.mark.parametrize(
    "private_format,encrypted",
    [
        (serialization.PrivateFormat.PKCS8, False),
        (serialization.PrivateFormat.TraditionalOpenSSL, False),
        (serialization.PrivateFormat.PKCS8, True),
    ],
)
def test_binary_private_key_cannot_be_mistaken_for_rotation_lineage(private_format, encrypted):
    # Ephemeral test keys never leave memory or reach a provider.
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ephemeral architecture QA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2030, 1, 1, tzinfo=UTC))
        .sign(key, hashes.SHA256())
    )
    encryption = (
        serialization.BestAvailableEncryption(b"synthetic-password")
        if encrypted
        else serialization.NoEncryption()
    )
    key_bytes = key.private_bytes(serialization.Encoding.DER, private_format, encryption)
    body = {
        "keyRotationReason": "ROUTINE_KEY_UPGRADE",
        "rotatedCloudKmsKey": {
            "cloudKmsKeyAndCert": {
                "cloudKmsKey": {"cryptoKeyVersionResource": KMS},
                "pemCertificate": base64.b64encode(
                    cert.public_bytes(serialization.Encoding.PEM)
                ).decode(),
            },
            "signingCertificateLineage": base64.b64encode(key_bytes).decode(),
        },
    }
    auth = Mock()
    service = signing.Signing(
        signing.SigningSettings(frozenset({APP}), frozenset({ROTATE}), frozenset({KMS})),
        auth,
        local=True,
    )
    with pytest.raises(PlayError):
        service.prepare(ROTATE, {"name": APP}, body, True)
    auth.token.assert_not_called()
    assert not service._preparations


def test_hosted_skips_every_privileged_environment_parser(monkeypatch):
    for name in (
        "GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPERS",
        "GOOGLE_PLAY_ACCOUNT_ACCESS_METHODS",
        "GOOGLE_PLAY_SIGNING_APPS",
        "GOOGLE_PLAY_SIGNING_METHODS",
        "GOOGLE_PLAY_SIGNING_KMS_VERSIONS",
        "GOOGLE_PLAY_APPSTORE_STORES",
        "GOOGLE_PLAY_APPSTORE_METHODS",
    ):
        monkeypatch.setenv(name, "invalid-but-hosted-must-not-read")
    for factory in ("build_account_tools", "appstore_tools", "signing_tools"):
        monkeypatch.setattr(
            "pubship.server." + factory,
            Mock(side_effect=AssertionError("Privileged local factory invoked by hosted server")),
        )
    server = create_server(service_factory=lambda: (Settings(), Mock()))

    async def run():
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert not names & PRIVILEGED_TOOLS

    asyncio.run(run())


@pytest.mark.parametrize("offset", ["+00:99", "-01:60", "+24:00"])
def test_account_expiration_offset_components_do_not_normalize(offset):
    from pubship.account_access_contracts import request_spec

    with pytest.raises(PlayError):
        request_spec(
            "androidpublisher.users.patch",
            {
                "name": "developers/123456789/users/synthetic@example.test",
                "updateMask": "expirationTime",
            },
            {"expirationTime": "2099-01-01T00:00:00" + offset},
        )
