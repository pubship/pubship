"""Independent signing boundaries; every credential and provider response is synthetic."""

import asyncio
import base64
import io
import json
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
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

APP = "com.example.signing"
KMS = "projects/example/locations/global/keyRings/testing/cryptoKeys/signing/cryptoKeyVersions/7"
TOKEN = "SYNTHETIC_ORIGINAL_SIGNING_CREDENTIAL"
ENROLL = signing.PREFIX + "enrollApp"
ROTATE = signing.PREFIX + "rotateAppSigningKey"


@pytest.fixture(scope="module")
def public_certificate():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic signing QA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2030, 1, 1, tzinfo=UTC))
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(certificate.public_bytes(serialization.Encoding.PEM)).decode()


@pytest.fixture
def intent(public_certificate):
    return {
        "enrollNewApp": {
            "cloudKmsKeyAndCert": {
                "cloudKmsKey": {"cryptoKeyVersionResource": KMS},
                "pemCertificate": public_certificate,
            }
        }
    }


@pytest.fixture
def service():
    auth = Mock()
    auth.token.return_value = TOKEN
    settings = signing.SigningSettings(
        frozenset({APP}), frozenset({ENROLL, ROTATE}), frozenset({KMS})
    )
    return signing.Signing(settings, auth, local=True)


def prepare(service, intent):
    return service.prepare(ENROLL, {"name": APP}, intent, True)["operation_id"]


def response_for(spec):
    return deepcopy(spec["certificates"])


def test_private_intent_is_frozen_and_original_credential_is_retained(service, intent, monkeypatch):
    operation = prepare(service, intent)
    frozen = deepcopy(intent)
    intent["enrollNewApp"]["cloudKmsKeyAndCert"]["cloudKmsKey"]["cryptoKeyVersionResource"] = (
        KMS + "9"
    )
    service.auth.token.return_value = "SYNTHETIC_REPLACEMENT_CREDENTIAL"
    observed = []

    def execute(spec, token):
        observed.append((deepcopy(spec), token))
        return response_for(spec)

    monkeypatch.setattr(signing, "execute", execute)
    result = service.apply(operation, operation)
    assert observed[0][0]["body"] == frozen
    assert observed[0][1] == TOKEN
    service.auth.token.assert_called_once_with("publisher")
    assert result["mutation_succeeded"] is True
    assert result["readback_available"] is False
    assert result["availability_verified"] is False
    assert TOKEN not in repr(result)
    assert frozen["enrollNewApp"]["cloudKmsKeyAndCert"]["pemCertificate"] not in repr(result)


@pytest.mark.parametrize("grant", ["apps", "methods", "kms_versions"])
def test_revoking_any_grant_consumes_id_without_provider_access(
    service, intent, monkeypatch, grant
):
    operation = prepare(service, intent)
    current = {
        "apps": service.settings.apps,
        "methods": service.settings.methods,
        "kms_versions": service.settings.kms_versions,
    }
    current[grant] = frozenset()
    service.settings = signing.SigningSettings(**current)
    execute = Mock()
    monkeypatch.setattr(signing, "execute", execute)
    with pytest.raises(PlayError, match="grants"):
        service.apply(operation, operation)
    with pytest.raises(PlayError, match="Unknown|consumed"):
        service.apply(operation, operation)
    execute.assert_not_called()


def test_bad_confirmation_does_not_destroy_live_intent(service, intent, monkeypatch):
    operation = prepare(service, intent)
    monkeypatch.setattr(signing, "execute", lambda spec, token: response_for(spec))
    with pytest.raises(PlayError, match="Confirmation"):
        service.apply(operation, "wrong")
    assert service.apply(operation, operation)["mutation_succeeded"]


def test_global_apply_contention_consumes_only_competing_operation(service, intent, monkeypatch):
    operation = prepare(service, intent)
    execute = Mock()
    monkeypatch.setattr(signing, "execute", execute)
    assert signing._APPLY_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(PlayError, match="Another apply"):
            service.apply(operation, operation)
    finally:
        signing._APPLY_LOCK.release()
    with pytest.raises(PlayError, match="Unknown|consumed"):
        service.apply(operation, operation)
    execute.assert_not_called()


def test_concurrent_apply_of_same_id_sends_exactly_once(service, intent, monkeypatch):
    operation = prepare(service, intent)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def execute(spec, token):
        calls.append(token)
        entered.set()
        assert release.wait(3)
        return response_for(spec)

    monkeypatch.setattr(signing, "execute", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.apply, operation, operation)
        assert entered.wait(3)
        try:
            with pytest.raises(PlayError, match="Unknown|consumed"):
                service.apply(operation, operation)
        finally:
            release.set()
        assert first.result(timeout=3)["mutation_succeeded"]
    assert calls == [TOKEN]


@pytest.mark.parametrize("unknown", [False, True])
def test_apply_prunes_other_expired_secrets_but_preserves_live_intent(
    service, intent, monkeypatch, unknown
):
    clock = [100.0]
    monkeypatch.setattr(signing.time, "monotonic", lambda: clock[0])
    expired = prepare(service, intent)
    clock[0] = 200.0
    live = prepare(service, intent)
    clock[0] = 701.0
    requested = "signing_" + "z" * 43 if unknown else live
    monkeypatch.setattr(signing, "execute", lambda spec, token: response_for(spec))
    if unknown:
        with pytest.raises(PlayError, match="Unknown|consumed"):
            service.apply(requested, requested)
        assert live in service._preparations
    else:
        assert service.apply(requested, requested)["mutation_succeeded"]
    assert expired not in service._preparations


def test_expiry_during_authorization_leaves_no_preparation(service, intent, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(signing.time, "monotonic", lambda: clock[0])

    def slow_auth(scope):
        clock[0] += 600
        return TOKEN

    service.auth.token.side_effect = slow_auth
    with pytest.raises(PlayError, match="expired"):
        prepare(service, intent)
    assert service._preparations == {}


def test_operation_id_collision_preserves_both_preparations(service, intent, monkeypatch):
    ids = iter(["a" * 43, "a" * 43, "b" * 43])
    monkeypatch.setattr(signing.secrets, "token_urlsafe", lambda size: next(ids))
    first = prepare(service, intent)
    second = prepare(service, intent)
    assert first != second
    assert set(service._preparations) == {first, second}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [],
        {"signingCertificate": {}},
        {"signingCertificate": {"certificateHashSha256": "AA" * 32}},
        {"instruction": "visit https://untrusted.example/"},
    ],
)
def test_malformed_mutation_response_consumes_id_and_releases_global_lock(
    service, intent, monkeypatch, bad
):
    operation = prepare(service, intent)
    monkeypatch.setattr(signing, "execute", lambda spec, token: bad)
    with pytest.raises(PlayError, match="outcome unknown") as error:
        service.apply(operation, operation)
    assert "untrusted" not in str(error.value)
    with pytest.raises(PlayError, match="Unknown|consumed"):
        service.apply(operation, operation)
    assert signing._APPLY_LOCK.acquire(blocking=False)
    signing._APPLY_LOCK.release()


@pytest.mark.parametrize(
    "algorithm", ["certificateHashMd5", "certificateHashSha1", "certificateHashSha256"]
)
def test_every_returned_digest_is_correlated_to_the_supplied_certificate(intent, algorithm):
    spec = signing.request_spec(ENROLL, {"name": APP}, intent)
    response = response_for(spec)
    response["signingCertificate"][algorithm] = "00" * (
        len(response["signingCertificate"][algorithm]) // 2
    )
    with pytest.raises(PlayError, match="outcome unknown"):
        signing.validate_response(spec, response)


def test_response_cannot_substitute_upload_certificate_for_signing_certificate(
    intent, public_certificate
):
    intent["pemUploadCertificate"] = public_certificate
    spec = signing.request_spec(ENROLL, {"name": APP}, intent)
    response = response_for(spec)
    del response["signingCertificate"]
    with pytest.raises(PlayError, match="outcome unknown"):
        signing.validate_response(spec, response)


class Response(io.BytesIO):
    status = 200


@pytest.mark.parametrize(
    "failure", ["timeout", "redirect", "server", "truncated", "oversized", "nonfinite"]
)
def test_transport_failures_are_uncertain_redacted_and_never_retried(intent, monkeypatch, failure):
    spec = signing.request_spec(ENROLL, {"name": APP}, intent)
    marker = "SYNTHETIC_PROVIDER_SECRET_INSTRUCTION"
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        assert timeout == 15
        assert request.method == "POST"
        assert (
            request.full_url
            == "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
            + APP
            + "/appSigning:enrollApp"
        )
        assert request.get_header("Authorization") == "Bearer " + TOKEN
        if failure == "timeout":
            raise TimeoutError(marker)
        if failure in {"redirect", "server"}:
            raise urllib.error.HTTPError(
                request.full_url,
                302 if failure == "redirect" else 503,
                marker,
                {"Location": "https://untrusted.example/"},
                io.BytesIO(marker.encode()),
            )
        return Response(
            {
                "truncated": b'{"signingCertificate":',
                "oversized": b" " * 65537,
                "nonfinite": b'{"signingCertificate":NaN}',
            }[failure]
        )

    def opener(handler, *bounded_handlers):
        assert isinstance(handler, signing.NoRedirect) and len(bounded_handlers) == 2
        return type("SyntheticOpener", (), {"open": staticmethod(open_request), "handlers": []})()

    monkeypatch.setattr(signing.urllib.request, "build_opener", opener)
    with pytest.raises(PlayError, match="outcome unknown") as error:
        signing.execute(spec, TOKEN)
    assert marker not in str(error.value)
    assert TOKEN not in str(error.value)
    assert "untrusted.example" not in str(error.value)
    assert len(calls) == 1


def test_preview_and_pending_repr_do_not_expose_certificate_bytes_or_credential(service, intent):
    result = service.prepare(ENROLL, {"name": APP}, intent, True)
    pending = service._preparations[result["operation_id"]]
    assert TOKEN not in repr(pending)
    assert TOKEN not in json.dumps(result)
    assert intent["enrollNewApp"]["cloudKmsKeyAndCert"]["pemCertificate"] not in repr(pending)
    assert intent["enrollNewApp"]["cloudKmsKeyAndCert"]["pemCertificate"] not in json.dumps(result)


@pytest.mark.parametrize("ack", [False, None, 1, "true"])
def test_acknowledgement_is_exact_boolean_before_auth(service, intent, ack):
    with pytest.raises(PlayError, match="acknowledge_signing_effects"):
        service.prepare(ENROLL, {"name": APP}, intent, ack)
    service.auth.token.assert_not_called()


def test_hosted_or_truthy_nonlocal_service_never_prepares(service, intent):
    for local in (False, 1, "true"):
        service.local = local
        with pytest.raises(PlayError, match="local"):
            prepare(service, intent)
    service.auth.token.assert_not_called()


def rotation_intent(intent, lineage):
    return {
        "keyRotationReason": "ROUTINE_KEY_UPGRADE",
        "rotatedCloudKmsKey": {
            "cloudKmsKeyAndCert": deepcopy(intent["enrollNewApp"]["cloudKmsKeyAndCert"]),
            "signingCertificateLineage": base64.b64encode(lineage).decode(),
        },
    }


@pytest.mark.parametrize("kind", ["pkcs8", "traditional", "encrypted", "certificate"])
def test_lineage_rejects_pem_instead_of_sending_mistaken_key_material(service, intent, kind):
    if kind == "certificate":
        lineage = base64.b64decode(intent["enrollNewApp"]["cloudKmsKeyAndCert"]["pemCertificate"])
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        lineage = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL
            if kind == "traditional"
            else serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"synthetic-test-password")
            if kind == "encrypted"
            else serialization.NoEncryption(),
        )
    with pytest.raises(PlayError, match="Lineage") as error:
        service.prepare(ROTATE, {"name": APP}, rotation_intent(intent, lineage), True)
    assert "synthetic-test-password" not in str(error.value)
    service.auth.token.assert_not_called()


def configure_signing(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_SIGNING_APPS", APP)
    monkeypatch.setenv("GOOGLE_PLAY_SIGNING_METHODS", ENROLL + "," + ROTATE)
    monkeypatch.setenv("GOOGLE_PLAY_SIGNING_KMS_VERSIONS", KMS)


@pytest.mark.parametrize("mode", ["new", "existing", "rotate"])
def test_real_local_mcp_signing_prepare_apply_replay_is_redacted(intent, monkeypatch, mode):
    configure_signing(monkeypatch)
    token = Mock(return_value=TOKEN)
    monkeypatch.setattr("pubship.server.Auth.token", token)
    calls = []

    def execute(spec, supplied_token):
        calls.append((deepcopy(spec), supplied_token))
        return response_for(spec) or {"signingCertificate": {"certificateHashSha256": "AB" * 32}}

    monkeypatch.setattr(signing, "execute", execute)
    selected = intent
    if mode == "existing":
        selected = {"enrollExistingApp": {"cloudKmsKey": {"cryptoKeyVersionResource": KMS}}}
    if mode == "rotate":
        selected = rotation_intent(intent, b"synthetic provider-validated lineage")
    method = ROTATE if mode == "rotate" else ENROLL

    async def run():
        async with Client(create_server(Settings())) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert tools["prepare_signing_operation"].annotations.read_only_hint
            assert tools["apply_signing_operation"].annotations.destructive_hint
            assert not tools["apply_signing_operation"].annotations.idempotent_hint
            prepared = await client.call_tool(
                "prepare_signing_operation",
                {
                    "method": method,
                    "parameters": {"name": APP},
                    "body": selected,
                    "acknowledge_signing_effects": True,
                },
            )
            assert not prepared.is_error, prepared.content
            operation = prepared.structured_content["operation_id"]
            applied = await client.call_tool(
                "apply_signing_operation",
                {"operation_id": operation, "confirmation": operation},
            )
            assert not applied.is_error, applied.content
            assert applied.structured_content["mutation_succeeded"]
            assert not applied.structured_content["readback_available"]
            assert not applied.structured_content["availability_verified"]
            replay = await client.call_tool(
                "apply_signing_operation",
                {"operation_id": operation, "confirmation": operation},
            )
            assert replay.is_error
            for result in (prepared, applied, replay):
                text = json.dumps(result.model_dump(mode="json"))
                assert TOKEN not in text
                assert intent["enrollNewApp"]["cloudKmsKeyAndCert"]["pemCertificate"] not in text
                assert (
                    base64.b64encode(b"synthetic provider-validated lineage").decode() not in text
                )

    asyncio.run(run())
    token.assert_called_once_with("publisher")
    assert len(calls) == 1
    assert calls[0][0]["body"] == selected
    assert calls[0][1] == TOKEN


def test_mcp_strict_arguments_and_cross_app_rejection_do_not_resolve_credentials(
    intent, monkeypatch
):
    configure_signing(monkeypatch)
    token = Mock(side_effect=AssertionError("Invalid MCP inputs must not authenticate"))
    monkeypatch.setattr("pubship.server.Auth.token", token)
    arguments = {
        "method": ENROLL,
        "parameters": {"name": APP},
        "body": intent,
        "acknowledge_signing_effects": True,
    }
    private = "SYNTHETIC_REJECTED_PRIVATE_INPUT"

    async def run():
        async with Client(create_server(Settings())) as client:
            for invalid in (
                {**arguments, "acknowledge_signing_effects": "true"},
                {**arguments, "extra": private},
                {**arguments, "parameters": {"name": "com.example.other"}},
                {**arguments, "body": {"privateKey": private}},
            ):
                result = await client.call_tool("prepare_signing_operation", invalid)
                assert result.is_error
                assert private not in json.dumps(result.model_dump(mode="json"))

    asyncio.run(run())
    token.assert_not_called()


def test_hosted_factory_never_inherits_complete_signing_environment(monkeypatch):
    configure_signing(monkeypatch)
    factory = Mock(side_effect=AssertionError("Tool discovery must not resolve accounts"))

    async def run():
        async with Client(create_server(service_factory=factory)) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert "prepare_signing_operation" not in tools
            assert "apply_signing_operation" not in tools

    asyncio.run(run())
    factory.assert_not_called()
