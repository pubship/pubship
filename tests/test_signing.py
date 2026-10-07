"""Enterprise signing contracts and one-shot execution; synthetic certificates only."""

import base64
import json
import urllib.error
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from cryptography.x509.oid import NameOID

from pubship import signing as s
from pubship.errors import PlayError
from pubship.publishing import _APPLY_LOCK

APP = "com.example.demo"
KMS = "projects/example-project/locations/global/keyRings/ring/cryptoKeys/key/cryptoKeyVersions/1"
PARAMS = {"name": APP}


@pytest.fixture(scope="module")
def cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic signing QA")])
    c = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(c.public_bytes(Encoding.PEM)).decode()


def body(cert, mode):
    kms = {"cryptoKeyVersionResource": KMS}
    pair = {"cloudKmsKey": kms, "pemCertificate": cert}
    if mode == "existing":
        return {"enrollExistingApp": {"cloudKmsKey": kms}}
    if mode == "rotate":
        return {
            "rotatedCloudKmsKey": {
                "cloudKmsKeyAndCert": pair,
                "signingCertificateLineage": base64.b64encode(
                    b"synthetic provider-validated lineage"
                ).decode(),
            },
            "keyRotationReason": "ROUTINE_KEY_UPGRADE",
        }
    return {"enrollNewApp": {"cloudKmsKeyAndCert": pair}, "pemUploadCertificate": cert}


def method(mode):
    return s.PREFIX + ("rotateAppSigningKey" if mode == "rotate" else "enrollApp")


def response(spec):
    return spec["certificates"] or {"signingCertificate": {"certificateHashSha256": "AB" * 32}}


def service():
    auth = SimpleNamespace(token=Mock(return_value="synthetic-secret-token"))
    return s.Signing(
        s.SigningSettings(frozenset({APP}), s.METHODS, frozenset({KMS})), auth, local=True
    )


@pytest.mark.parametrize("mode", ["new", "existing", "rotate"])
def test_prepare_apply_copies_correlates_and_consumes(cert, mode, monkeypatch):
    svc = service()
    intent = body(cert, mode)
    prepared = svc.prepare(method(mode), PARAMS, intent, True)
    intent.clear()
    executor = Mock(side_effect=lambda spec, token: response(spec))
    monkeypatch.setattr(s, "execute", executor)
    result = svc.apply(prepared["operation_id"], prepared["operation_id"])
    assert result["mutation_succeeded"] and not result["availability_verified"]
    assert not result["readback_available"]
    assert executor.call_args.args[1] == "synthetic-secret-token"
    assert svc.auth.token.call_count == 1
    assert cert not in json.dumps(prepared)
    assert "synthetic-secret-token" not in json.dumps(result)
    with pytest.raises(PlayError, match="consumed"):
        svc.apply(prepared["operation_id"], prepared["operation_id"])


@pytest.mark.parametrize("mode", ["new", "existing", "rotate"])
def test_fixed_transport_and_correlated_response(cert, mode, monkeypatch):
    spec = s.request_spec(method(mode), PARAMS, body(cert, mode))
    reply = Mock(status=200)
    reply.read.return_value = json.dumps(response(spec)).encode()
    reply.__enter__ = Mock(return_value=reply)
    reply.__exit__ = Mock(return_value=False)
    opener = Mock(handlers=[])
    opener.open.return_value = reply
    monkeypatch.setattr(s.urllib.request, "build_opener", Mock(return_value=opener))
    result = s.execute(spec, "synthetic-secret-token")
    req = opener.open.call_args.args[0]
    assert req.method == "POST"
    assert (
        req.full_url
        == "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        + APP
        + "/appSigning:"
        + method(mode).removeprefix(s.PREFIX)
    )
    assert json.loads(req.data) == spec["body"]
    assert result == response(spec)


@pytest.mark.parametrize(
    "change",
    ["missing", "both", "kms", "version", "cert", "private", "field", "name", "url", "nan"],
)
def test_invalid_enrollment_before_auth(cert, change):
    svc = service()
    b = body(cert, "new")
    p = dict(PARAMS)
    pair = b["enrollNewApp"]["cloudKmsKeyAndCert"]
    if change == "missing":
        b = {}
    if change == "both":
        b.update(body(cert, "existing"))
    if change == "kms":
        pair["cloudKmsKey"] = {}
    if change == "version":
        pair["cloudKmsKey"]["cryptoKeyVersionResource"] = KMS.rsplit("/", 1)[0] + "/latest"
    if change == "cert":
        pair["pemCertificate"] = base64.b64encode(b"not a certificate").decode()
    if change == "private":
        pair["pemCertificate"] = base64.b64encode(
            rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            )
        ).decode()
    if change == "field":
        b["privateKey"] = "secret"
    if change == "name":
        p["name"] = "../other"
    if change == "url":
        p["name"] = "https://attacker.invalid"
    if change == "nan":
        b["pemUploadCertificate"] = float("nan")
    with pytest.raises(PlayError):
        svc.prepare(method("new"), p, b, True)
    svc.auth.token.assert_not_called()


@pytest.mark.parametrize("key", ["rotatedCloudKmsKey", "keyRotationReason"])
def test_rotation_required(cert, key):
    b = body(cert, "rotate")
    del b[key]
    with pytest.raises(PlayError):
        s.request_spec(method("rotate"), PARAMS, b)


def test_rotation_reason_lineage_and_upload(cert):
    for edit in (
        lambda b: b.update(keyRotationReason="KEY_ROTATION_REASON_UNSPECIFIED"),
        lambda b: b["rotatedCloudKmsKey"].update(signingCertificateLineage=""),
        lambda b: b["rotatedCloudKmsKey"].update(signingCertificateLineage="dGVzdA==\n"),
    ):
        b = body(cert, "rotate")
        edit(b)
        with pytest.raises(PlayError):
            s.request_spec(method("rotate"), PARAMS, b)


@pytest.mark.parametrize("kind", ["wrong", "missing", "extra", "malformed", "empty", "unknown"])
def test_mutation_response_fail_closed(cert, kind):
    spec = s.request_spec(method("new"), PARAMS, body(cert, "new"))
    data = deepcopy(response(spec))
    if kind == "wrong":
        data["signingCertificate"]["certificateHashSha256"] = "00" * 32
    if kind == "missing":
        del data["uploadCertificate"]
    if kind == "extra":
        data["other"] = "secret"
    if kind == "malformed":
        data["signingCertificate"]["certificateHashSha256"] = "oops"
    if kind == "empty":
        data["signingCertificate"] = {}
    if kind == "unknown":
        data["signingCertificate"]["other"] = "secret"
    with pytest.raises(PlayError, match="outcome unknown") as caught:
        s.validate_response(spec, data)
    assert "secret" not in str(caught.value)


def test_colon_hashes(cert):
    spec = s.request_spec(method("new"), PARAMS, body(cert, "new"))
    data = {
        field: {
            key: ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).lower()
            for key, digest in values.items()
        }
        for field, values in response(spec).items()
    }
    assert s.validate_response(spec, data) == response(spec)


@pytest.mark.parametrize("fault", ["expiry", "grant", "lock", "transport"])
def test_failure_consumes_id_without_retry(cert, fault, monkeypatch):
    svc = service()
    p = svc.prepare(method("new"), PARAMS, body(cert, "new"), True)
    ident = p["operation_id"]
    executor = Mock(side_effect=PlayError(s.UNKNOWN))
    monkeypatch.setattr(s, "execute", executor)
    if fault == "expiry":
        svc._preparations[ident].deadline = 0
    if fault == "grant":
        svc.settings = s.SigningSettings()
    if fault == "lock":
        _APPLY_LOCK.acquire()
    try:
        with pytest.raises(PlayError):
            svc.apply(ident, ident)
    finally:
        if fault == "lock":
            _APPLY_LOCK.release()
    assert executor.call_count == (1 if fault == "transport" else 0)
    with pytest.raises(PlayError, match="consumed"):
        svc.apply(ident, ident)


def test_local_optin_capacity_confirmation(cert, monkeypatch):
    svc = service()
    svc.local = False
    with pytest.raises(PlayError):
        svc.prepare(method("new"), PARAMS, body(cert, "new"), True)
    svc.local = True
    with pytest.raises(PlayError):
        svc.prepare(method("new"), PARAMS, body(cert, "new"))
    svc.auth.token.assert_not_called()
    monkeypatch.setattr(s, "MAX_PREPARATIONS", 1)
    p = svc.prepare(method("new"), PARAMS, body(cert, "new"), True)
    with pytest.raises(PlayError, match="capacity"):
        svc.prepare(method("new"), PARAMS, body(cert, "new"), True)
    with pytest.raises(PlayError, match="Confirmation"):
        svc.apply(p["operation_id"], "wrong")
    assert p["operation_id"] in svc._preparations
    assert "synthetic-secret-token" not in repr(svc._preparations[p["operation_id"]])


@pytest.mark.parametrize(
    "key,value",
    [
        ("apps", frozenset({"*"})),
        ("apps", {APP}),
        ("methods", frozenset({"androidpublisher.users.delete"})),
        ("kms_versions", frozenset({KMS + "/.."})),
    ],
)
def test_config_rejects_broad_or_wrong_scope(key, value):
    with pytest.raises(PlayError):
        s.SigningSettings(**{key: value})


def test_signing_scopes_independent(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", APP)
    assert s.SigningSettings.from_env() == s.SigningSettings()
    for key, value in (("APPS", APP), ("METHODS", method("new")), ("KMS_VERSIONS", KMS)):
        monkeypatch.setenv("GOOGLE_PLAY_SIGNING_" + key, value)
    assert s.SigningSettings.from_env().apps == frozenset({APP})


@pytest.mark.parametrize("fault", ["redirect", "timeout", "large", "json", "204"])
def test_transport_failures_redacted(cert, fault, monkeypatch):
    spec = s.request_spec(method("existing"), PARAMS, body(cert, "existing"))
    reply = Mock(status=204 if fault == "204" else 200)
    reply.read.return_value = (
        b"x" * 65537 if fault == "large" else b"not JSON private-provider-text"
    )
    reply.__enter__ = Mock(return_value=reply)
    reply.__exit__ = Mock(return_value=False)
    opener = Mock(handlers=[])
    opener.open.return_value = reply
    if fault == "redirect":
        opener.open.side_effect = urllib.error.HTTPError(
            "https://private.invalid", 302, "secret", {}, None
        )
    if fault == "timeout":
        opener.open.side_effect = TimeoutError("private-provider-text")
    monkeypatch.setattr(s.urllib.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(PlayError, match="outcome unknown") as caught:
        s.execute(spec, "synthetic-secret-token")
    assert "private" not in str(caught.value)
    assert opener.open.call_count == 1
