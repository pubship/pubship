"""Independent account/signing acceptance over actual TLS and official MCP/OAuth."""

import asyncio
import base64
import hashlib
import io
import json
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from test_hosted_020_loopback import client, successful
from test_hosted_020_loopback import loopback as loopback  # noqa: F401
from test_hosted_021_consent import hidden
from test_hosted_consent import finish, request_consent
from test_signing import body as signing_body
from test_signing import cert as cert  # noqa: F401

from pubship.hosted_grants import (
    ACCOUNT_DIRECTORY,
    ACCOUNT_GRANTS,
    ACCOUNT_USERS,
    ADMIN_SCOPES,
    READ_SCOPE,
    SIGNING_ENROLL,
    SIGNING_ROTATE,
)

P = "androidpublisher."
D, D2 = "123456789", "987654321"
U, U2 = "Target@example.test", "other@example.test"
APP, APP2 = "com.example.target", "com.example.other"
KMS = "projects/example-project/locations/global/keyRings/ring/cryptoKeys/key/cryptoKeyVersions/1"
KMS2 = KMS[:-1] + "2"
NAME = f"developers/{D}/users/{U}"
GRANT = NAME + "/grants/" + APP
DEV_PERM, APP_PERM = "CAN_VIEW_NON_FINANCIAL_DATA_GLOBAL", "CAN_VIEW_NON_FINANCIAL_DATA"
USER = {
    "name": NAME,
    "email": U,
    "developerAccountPermissions": [DEV_PERM],
    "grants": [{"name": GRANT, "packageName": APP, "appLevelPermissions": [APP_PERM]}],
}
UNRELATED = {
    "name": f"developers/{D}/users/private-unrelated@example.test",
    "email": "private-unrelated@example.test",
}
ACCOUNT_METHODS = tuple(
    P + name
    for name in (
        "users.list",
        "users.create",
        "users.patch",
        "users.delete",
        "grants.create",
        "grants.patch",
        "grants.delete",
    )
)
ROTATION_REASONS = (
    "COMPROMISED_KEY",
    "USE_STRONGER_KEY",
    "USE_SAME_KEY_FOR_MULTIPLE_APPS",
    "ROUTINE_KEY_UPGRADE",
    "OTHER",
)
SIGNING_CASES = [
    (mode, upload, None) for mode in ("new", "existing") for upload in (False, True)
] + [("rotate", False, reason) for reason in ROTATION_REASONS]


def consent_fields(scopes=ADMIN_SCOPES):
    fields = {"packages": "", "services": "publisher", "capabilities": sorted(scopes)}
    if ACCOUNT_DIRECTORY in scopes:
        fields["directory"] = D
    for i, (dev, user, app, key) in enumerate(((D, U, APP, KMS), (D2, U2, APP2, KMS2))):
        for cap in (ACCOUNT_USERS, ACCOUNT_GRANTS):
            if cap not in scopes:
                continue
            prefix = cap.removeprefix("play:") + f"[{i}]"
            fields.update({prefix + "[developer]": dev, prefix + "[user]": user})
            family = "users" if cap == ACCOUNT_USERS else "grants"
            fields[prefix + "[methods]"] = ",".join(
                P + family + "." + operation for operation in ("create", "patch", "delete")
            )
            if cap == ACCOUNT_USERS:
                fields.update(
                    {
                        prefix + "[developer_permissions]": DEV_PERM,
                        prefix + "[allow_expiration_change]": "yes",
                        prefix + "[affected_apps][0][app]": app,
                        prefix + "[affected_apps][0][permissions]": APP_PERM,
                    }
                )
            else:
                fields.update({prefix + "[app]": app, prefix + "[app_permissions]": APP_PERM})
        for cap in (SIGNING_ENROLL, SIGNING_ROTATE):
            if cap in scopes:
                prefix = cap.removeprefix("play:") + f"[{i}]"
                fields.update({prefix + "[app]": app, prefix + "[kms_version]": key})
    return fields


def connect(network, *, scopes=ADMIN_SCOPES, fields=None, request=None):
    requested = sorted({READ_SCOPE, *scopes})
    if request is None:
        url, _, request = request_consent(network.browser, network.settings, requested)
    else:
        request = deepcopy(request)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(request["verifier"].encode()).digest())
            .decode()
            .rstrip("=")
        )
        response = network.browser.get(
            "/authorize",
            params={
                "client_id": request["client_id"],
                "redirect_uri": request["redirect"],
                "response_type": "code",
                "scope": " ".join(requested),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "client-state",
                "resource": network.settings.resource,
            },
        )
        assert response.status_code == 302
        url = response.headers["location"]
        assert network.browser.get(url).status_code == 200
    review = network.browser.post(
        url,
        data=consent_fields(scopes) if fields is None else fields,
        headers={"Origin": network.settings.issuer},
    )
    assert review.status_code == 200 and "Review exact connection authority" in review.text
    selected = network.browser.post(
        url, data=hidden(review.text), headers={"Origin": network.settings.issuer}
    )
    return request, finish(network.browser, network.settings, request, selected)


def account_case(method):
    family, operation = method.rsplit(".", 2)[-2:]
    before, after = deepcopy(USER), deepcopy(USER)
    if operation == "list":
        parameters, body = {"parent": "developers/" + D, "pageSize": 2}, None
    elif family == "users":
        parameters = {"parent": "developers/" + D} if operation == "create" else {"name": NAME}
        body = None if operation == "delete" else {"developerAccountPermissions": [DEV_PERM]}
        if operation == "create":
            body.update(name=NAME, email=U)
            before = None
        elif operation == "patch":
            parameters["updateMask"] = "developerAccountPermissions"
        else:
            after = None
    else:
        parameters = {"parent": NAME} if operation == "create" else {"name": GRANT}
        body = None if operation == "delete" else {"appLevelPermissions": [APP_PERM]}
        if operation == "create":
            body.update(name=GRANT, packageName=APP)
            before["grants"] = []
        elif operation == "patch":
            parameters["updateMask"] = "appLevelPermissions"
        else:
            after["grants"] = []
    return SimpleNamespace(
        method=method,
        family=family,
        operation=operation,
        parameters=parameters,
        body=body,
        before=before,
        after=after,
        mutated=False,
        calls=[],
        on_read=None,
        on_write=None,
        page_override=None,
    )


def account_provider(monkeypatch, state):
    users_path = f"/androidpublisher/v3/developers/{D}/users"
    target_path = users_path + "/" + quote(U, safe="")
    mutation_path = (
        (users_path if state.operation == "create" else target_path)
        if state.family == "users"
        else target_path + "/grants" + ("" if state.operation == "create" else "/" + APP)
    )
    verb = {"create": "POST", "patch": "PATCH", "delete": "DELETE", "list": "GET"}[state.operation]

    def send(request, timeout):
        parsed = urlsplit(request.full_url)
        assert parsed.scheme == "https" and parsed.netloc == "androidpublisher.googleapis.com"
        assert request.get_header("Authorization") == "Bearer PRIVATE-GOOGLE-ALICE"
        query = parse_qs(parsed.query)
        body = json.loads(request.data) if request.data is not None else None
        state.calls.append((request.method, parsed.path, query, body))
        if request.method == "GET":
            assert parsed.path == users_path
            expected_size = "2" if state.operation == "list" else "100"
            assert query.get("pageSize") == [expected_size]
            assert set(query) <= {"pageSize", "pageToken"}
            page = query.get("pageToken", [None])[0]
            current = state.after if state.mutated else state.before
            payload = (
                {"users": [deepcopy(UNRELATED)], "nextPageToken": "synthetic-page-two"}
                if page is None
                else {"users": [] if current is None else [deepcopy(current)]}
            )
            assert page in (None, "synthetic-page-two")
            if state.page_override:
                payload = state.page_override(page, payload)
            if state.on_read:
                state.on_read(page)
        else:
            assert request.method == verb and parsed.path == mutation_path
            assert query == (
                {"updateMask": [state.parameters["updateMask"]]}
                if state.operation == "patch"
                else {}
            )
            assert body == state.body
            state.mutated = True
            payload = (
                {}
                if state.operation == "delete"
                else deepcopy(state.after)
                if state.family == "users"
                else deepcopy(state.after["grants"][0])
            )
            if state.on_write:
                state.on_write()
        response = io.BytesIO(json.dumps(payload).encode())
        response.status = 200
        return response

    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", lambda _, request, timeout: send(request, timeout)
    )
    return state


def signing_case(cert, mode="existing", upload=False, reason=None):
    body = signing_body(cert, mode)
    body.pop("pemUploadCertificate", None)
    if upload:
        body["pemUploadCertificate"] = cert
    if reason:
        body["keyRotationReason"] = reason
    method = P + "appsigning." + ("rotateAppSigningKey" if mode == "rotate" else "enrollApp")
    return SimpleNamespace(
        method=method,
        parameters={"name": APP},
        body=body,
        mode=mode,
        upload=upload,
        calls=[],
        on_write=None,
    )


def signing_provider(monkeypatch, state, cert):
    der = x509.load_pem_x509_certificate(base64.b64decode(cert)).public_bytes(Encoding.DER)
    hashes = {
        "certificateHash" + suffix: hashlib.new(name, der, usedforsecurity=False)
        .hexdigest()
        .upper()
        for suffix, name in (("Md5", "md5"), ("Sha1", "sha1"), ("Sha256", "sha256"))
    }
    result = {
        "rotatedKeyCertificate" if state.mode == "rotate" else "signingCertificate": hashes
        if state.mode != "existing"
        else {"certificateHashSha256": "AB" * 32}
    }
    if state.upload:
        result["uploadCertificate"] = hashes

    def send(_, request, timeout):
        state.calls.append(request)
        assert request.method == "POST"
        assert (
            request.full_url
            == "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
            + APP
            + "/appSigning:"
            + state.method.rsplit(".", 1)[1]
        )
        assert request.get_header("Authorization") == "Bearer PRIVATE-GOOGLE-ALICE"
        assert json.loads(request.data) == state.body
        if state.on_write:
            state.on_write()
        response = io.BytesIO(json.dumps(result).encode())
        response.status = 200
        return response

    monkeypatch.setattr("urllib.request.OpenerDirector.open", send)
    return result


async def prepare_account(mcp, state):
    return await successful(
        mcp,
        "prepare_account_access",
        {
            "method": state.method,
            "parameters": state.parameters,
            "body": state.body,
            "acknowledge_access_effects": True,
        },
    )


async def prepare_signing(mcp, state):
    return await successful(
        mcp,
        "prepare_signing_operation",
        {
            "method": state.method,
            "parameters": state.parameters,
            "body": state.body,
            "acknowledge_signing_effects": True,
        },
    )


def apply_args(prepared):
    return {"operation_id": prepared["operation_id"], "confirmation": prepared["operation_id"]}


@pytest.mark.parametrize("method", ACCOUNT_METHODS)
def test_all_seven_account_methods_real_tls_oauth_exact_pages_and_single_write(
    loopback, monkeypatch, method, caplog
):
    _, token = connect(loopback)
    state = account_provider(monkeypatch, account_case(method))

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            assert len((await mcp.list_tools()).tools) == 46
            inventory = await successful(mcp, "list_api_methods", {"limit": 200})
            assert sum(row["execution"]["implemented"] for row in inventory["methods"]) == 170
            caps = await successful(mcp, "get_connection_capabilities", {})
            assert caps["capability_packages"] == {READ_SCOPE: []}
            if method == P + "users.list":
                result = await successful(
                    mcp, "read_account_access", {"method": method, "parameters": state.parameters}
                )
                assert result["response"]["users"] == [UNRELATED]
                assert len(state.calls) == 1
                return
            prepared = await prepare_account(mcp, state)
            assert len(state.calls) == 2 and all(c[0] == "GET" for c in state.calls)
            assert UNRELATED["email"] not in json.dumps(prepared)
            provider = loopback.app.state.oauth
            principal = await provider.load_access_token(token["access_token"])
            grant = provider.vault.get("grants", principal.subject)
            grant["access_token"] = "synthetic-new-credential-not-for-existing-operation"
            provider.vault.put("grants", principal.subject, grant, 3600)
            result = await successful(mcp, "apply_account_access", apply_args(prepared))
            assert result["mutation_succeeded"] and result["readback_succeeded"]
            assert len([c for c in state.calls if c[0] != "GET"]) == 1
            assert len(state.calls) == 7
            count = len(state.calls)
            assert (await mcp.call_tool("apply_account_access", apply_args(prepared))).is_error
            assert len(state.calls) == count
            assert UNRELATED["email"] not in json.dumps(result)
            assert not loopback.app.state.hosted_registry._records

    asyncio.run(exercise())
    assert "PRIVATE-GOOGLE" not in caplog.text and UNRELATED["email"] not in caplog.text


@pytest.mark.parametrize("mode,upload,reason", SIGNING_CASES)
def test_all_signing_forms_real_tls_with_exact_private_payload_no_invented_get(
    loopback, monkeypatch, cert, caplog, mode, upload, reason
):
    _, token = connect(loopback, scopes={SIGNING_ENROLL, SIGNING_ROTATE})
    state = signing_case(cert, mode, upload, reason)
    signing_provider(monkeypatch, state, cert)

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            prepared = await prepare_signing(mcp, state)
            assert not state.calls and cert not in json.dumps(prepared)
            provider = loopback.app.state.oauth
            principal = await provider.load_access_token(token["access_token"])
            grant = provider.vault.get("grants", principal.subject)
            grant["access_token"] = "synthetic-new-credential-not-for-existing-operation"
            provider.vault.put("grants", principal.subject, grant, 3600)
            result = await successful(mcp, "apply_signing_operation", apply_args(prepared))
            assert result["mutation_succeeded"] and result["readback_available"] is False
            assert len(state.calls) == 1 and cert not in json.dumps(result)
            if mode == "rotate":
                lineage = state.body["rotatedCloudKmsKey"]["signingCertificateLineage"]
                assert lineage not in json.dumps(prepared) + json.dumps(result) + caplog.text
            assert (await mcp.call_tool("apply_signing_operation", apply_args(prepared))).is_error
            assert len(state.calls) == 1 and not loopback.app.state.hosted_registry._records

    asyncio.run(exercise())
    assert (
        cert not in caplog.text and KMS not in caplog.text and "PRIVATE-GOOGLE" not in caplog.text
    )
