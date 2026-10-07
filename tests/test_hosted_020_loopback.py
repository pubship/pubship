"""Real TLS sockets and official MCP client; every Google response is synthetic.

Physical allocation is deliberately substituted on this portable fixture; the
separate Linux storage test proves real fallocate. No live provider is contacted.
"""

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
import uvicorn
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_appstore_contracts import APP, STORE, case
from test_artifacts_engineering import external
from test_hosted_consent import finish, request_consent

from pubship import hosted
from pubship.appstore_contracts import POLICY, READ_METHODS
from pubship.artifact_contracts import DOWNLOAD_METHODS, EXTERNAL
from pubship.hosted_config import HostedSettings, TransferLimits
from pubship.hosted_grants import APPSTORE_EVENTS, APPSTORE_PAIR_SCOPES, READ_SCOPE
from pubship.hosted_transfers import HostedTransferStore

P = "androidpublisher."
DATA = b"PK\x03\x04" + bytes(range(256)) * 1200  # Exceeds ordinary 256 KiB HTTP envelope.
OTHER = "com.example.other"
OTHER_STORE = "com.example.otherstore"
CAPS = {
    "play:edit-artifacts",
    "play:artifact-downloads",
    "play:internal-sharing",
    "play:appstore-catalog",
    "play:appstore-events",
    "play:appstore-management",
    "play:appstore-media",
}
ROUTES = {
    "edits.apks.upload": f"applications/{APP}/edits/edit1/apks",
    "edits.bundles.upload": f"applications/{APP}/edits/edit1/bundles",
    "edits.deobfuscationfiles.upload": f"applications/{APP}/edits/edit1/apks/7/deobfuscationFiles/proguard",
    "edits.expansionfiles.upload": f"applications/{APP}/edits/edit1/apks/7/expansionFiles/main",
    "edits.images.upload": f"applications/{APP}/edits/edit1/listings/en-US/icon",
    "edits.apks.addexternallyhosted": f"applications/{APP}/edits/edit1/apks/externallyHosted",
    "generatedapks.download": f"applications/{APP}/generatedApks/7/downloads/opaque-download:download",
    "systemapks.variants.download": f"applications/{APP}/systemApks/7/variants/1:download",
    "internalappsharingartifacts.uploadapk": f"applications/internalappsharing/{APP}/artifacts/apk",
    "internalappsharingartifacts.uploadbundle": f"applications/internalappsharing/{APP}/artifacts/bundle",
    "appstoreappsreview.createappstorehostedapp": f"appstore/{STORE}/apps:create",
    "appstoreappsreview.updateappstorehostedapp": f"appstore/{STORE}/apps:update",
    "appstoreappsreview.updateappstorehostedapppublishstatus": f"appstore/{STORE}/apps/{APP}:updateAppStoreHostedAppPublishStatus",
    "appstoreappsreview.uploadapk": f"appstore/{STORE}/apps/{APP}/apks:upload",
    "appstoreappsreview.uploadimage": f"appstore/{STORE}/apps/{APP}/images:upload",
    "appstoreappsreview.uploadappstoreapppolicydeclarationfile": f"appstore/{STORE}/apps/{APP}/policyDeclarationFiles:upload",
    "appstorecatalog.recentappviews.get": f"appstorecatalog/{STORE}/recentAppViews/{APP}",
    "appstorecatalog.recentupdateevents.list": f"appstorecatalog/{STORE}/recentUpdateEvents",
}
METHODS = tuple(P + name for name in ROUTES)


def certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic loopback")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = tmp_path / "key.pem", tmp_path / "cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path, ssl.create_default_context(cafile=str(cert_path))


@pytest.fixture
def loopback(tmp_path, monkeypatch, synthetic_google_identity):
    tmp_path.chmod(0o700)
    root = tmp_path / "spools"
    root.mkdir(mode=0o700)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    issuer = f"https://127.0.0.1:{sock.getsockname()[1]}"
    key, cert, tls = certificate(tmp_path)
    settings = HostedSettings(
        issuer,
        "synthetic-client",
        "synthetic-secret",
        tmp_path / "vault.db",
        Fernet.generate_key(),
        allowed_emails=frozenset({"owner@example.test"}),
        new_enrollment_enabled=True,
        transfer_directory=root,
        transfer_limits=TransferLimits(
            object_bytes=512 * 1024, connection_bytes=1024 * 1024, total_bytes=2 * 1024 * 1024
        ),
    )
    monkeypatch.setattr(
        hosted,
        "HostedTransferStore",
        lambda directory, limits: HostedTransferStore(
            directory, limits, _allocate=lambda fd, offset, size: os.ftruncate(fd, size)
        ),
    )
    monkeypatch.setattr(
        "pubship.server.Auth", Mock(side_effect=AssertionError("Local auth forbidden"))
    )
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open",
        Mock(side_effect=AssertionError("Live Google forbidden")),
    )
    app = hosted.create_hosted_app(settings)
    events, byte_authorizations = [], []

    async def observed(scope, receive, send):
        if scope.get("path", "").startswith("/transfers/"):
            byte_authorizations.append(
                (scope["method"], dict(scope["headers"]).get(b"authorization"))
            )

        async def counted():
            event = await receive()
            if scope.get("path", "").startswith("/transfers/"):
                events.append((scope["path"], event["type"]))
            return event

        await app(scope, counted, send)

    server = uvicorn.Server(
        uvicorn.Config(
            observed,
            log_level="critical",
            access_log=False,
            ssl_keyfile=str(key),
            ssl_certfile=str(cert),
            lifespan="on",
        )
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "Loopback server did not start"
    with httpx2.Client(
        base_url=issuer, verify=tls, trust_env=False, follow_redirects=False
    ) as browser:
        try:
            yield SimpleNamespace(
                settings=settings,
                app=app,
                browser=browser,
                tls=tls,
                events=events,
                byte_authorizations=byte_authorizations,
            )
        finally:
            browser.close()
            server.should_exit = True
            thread.join(5)
            sock.close()
            app.state.vault.close()
            assert not thread.is_alive(), "Loopback server failed to stop"


def connect(network, *, request=None, caps=CAPS, store_only=False):
    scopes = sorted({READ_SCOPE, *caps})
    if request is None:
        url, _, request = request_consent(network.browser, network.settings, scopes)
    else:
        request = dict(request)
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
                "scope": " ".join(scopes),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "client-state",
                "resource": network.settings.resource,
            },
        )
        assert response.status_code == 302
        url = response.headers["location"]
        assert network.browser.get(url).status_code == 200
    data = {
        "packages": "" if store_only else APP,
        "services": "publisher",
        "capabilities": sorted(caps),
    }
    for cap in caps:
        if cap in APPSTORE_PAIR_SCOPES:
            data[f"appstore_choices[{cap}]"] = f"{STORE}/{APP},{OTHER_STORE}/{OTHER}"
        elif cap == APPSTORE_EVENTS:
            data[f"appstore_choices[{cap}]"] = STORE
        else:
            data[f"capability_packages[{cap}]"] = APP
    selected = network.browser.post(url, data=data, headers={"Origin": network.settings.issuer})
    return request, finish(network.browser, network.settings, request, selected)


@asynccontextmanager
async def client(network, token):
    async with httpx2.AsyncClient(
        verify=network.tls,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + token["access_token"]},
    ) as http:
        async with Client(
            streamable_http_client(network.settings.resource, http_client=http)
        ) as mcp:
            yield mcp, http


def fixture_case(method):
    if ".appstore" in method:
        p, body, mime, result = case(method)
        return SimpleNamespace(
            method=method,
            kind="appstore_operation",
            parameters=p,
            body=body,
            mime=mime,
            result=result,
        )
    sharing = "internalappsharingartifacts" in method
    p = {"packageName": APP} if sharing else {"packageName": APP, "editId": "edit1"}
    body, mime, result = None, "application/octet-stream", {"versionCode": 7}
    if sharing:
        result = {"downloadUrl": "https://play.google.com/apps/test/sensitive-link"}
    elif method.endswith("images.upload"):
        p.update(language="en-US", imageType="icon")
        mime, result = "image/png", {"image": {"id": "image1"}}
    elif method.endswith("deobfuscationfiles.upload"):
        p.update(apkVersionCode=7, deobfuscationFileType="proguard")
        result = {"deobfuscationFile": {"symbolType": "proguard"}}
    elif method.endswith("expansionfiles.upload"):
        p.update(apkVersionCode=7, expansionFileType="main")
        result = {"expansionFile": {"fileSize": str(len(DATA))}}
    elif method == EXTERNAL:
        body, mime = external(), None
        body["externallyHostedApk"]["packageName"] = APP
        result = {"externallyHostedApk": {"packageName": APP, "versionCode": 7}}
    elif method in DOWNLOAD_METHODS:
        p = (
            {"packageName": APP, "versionCode": 7, "downloadId": "opaque-download"}
            if "generatedapks" in method
            else {"packageName": APP, "versionCode": "7", "variantId": 1}
        )
        mime = None
    return SimpleNamespace(
        method=method,
        kind="internal_sharing_upload" if sharing else "artifact_upload",
        parameters=p,
        body=body,
        mime=mime,
        result=result,
    )


def google(monkeypatch, state):
    edit_expiry = str(int(time.time()) + 3600)
    calls = []
    endpoint = (
        ("/upload" if state.mime else "/download" if state.method in DOWNLOAD_METHODS else "")
        + "/androidpublisher/v3/"
        + ROUTES[state.method.removeprefix(P)]
    )

    def send(req, timeout):
        assert req.get_header("Authorization") == "Bearer PRIVATE-GOOGLE-ALICE"
        parsed = urlsplit(req.full_url)
        assert parsed.scheme == "https" and parsed.netloc == "androidpublisher.googleapis.com"
        payload = req.data
        if hasattr(payload, "read"):
            content = bytearray()
            while chunk := payload.read(8192):
                content.extend(chunk)
            payload = bytes(content)
        calls.append((req.get_method(), parsed.path, parse_qs(parsed.query), payload))
        if parsed.path == endpoint:
            assert req.get_method() == (
                "GET" if state.method in DOWNLOAD_METHODS | READ_METHODS else "POST"
            )
            if state.mime:
                assert len(payload) == int(req.get_header("Content-length"))
                assert parse_qs(parsed.query) == {
                    "uploadType": ["multipart" if state.method == POLICY else "media"]
                }
                if state.method == POLICY:
                    message = BytesParser(policy=policy.default).parsebytes(
                        b"Content-Type: "
                        + req.get_header("Content-type").encode()
                        + b"\r\nMIME-Version: 1.0\r\n\r\n"
                        + payload
                    )
                    parts = list(message.iter_parts())
                    assert len(parts) == 2
                    assert json.loads(parts[0].get_payload(decode=True)) == state.body
                    assert parts[1].get_content_type() == state.mime
                    assert parts[1].get_payload(decode=True) == DATA
                else:
                    assert payload == DATA
                    assert req.get_header("Content-type") == state.mime
            elif state.method in DOWNLOAD_METHODS:
                assert parse_qs(parsed.query) == {"alt": ["media"]}
            elif state.method in READ_METHODS:
                assert parse_qs(parsed.query) == {
                    key: [value]
                    for key, value in state.parameters.items()
                    if key not in {"appStorePackageName", "playAppPackageName"}
                }
            else:
                assert not parsed.query
                assert json.loads(payload) == state.body
            result = state.result
        else:
            assert req.get_method() == "GET", "Unexpected provider mutation"
            edit = f"/androidpublisher/v3/applications/{APP}/edits/edit1"
            assert parsed.path.startswith(edit)
            suffix = parsed.path.removeprefix(edit)
            result = (
                {"id": "edit1", "expiryTimeSeconds": edit_expiry}
                if not suffix
                else {"listings": [{"language": "en-US"}]}
                if suffix == "/listings"
                else {"images": [{"id": "image1"}]}
                if suffix.startswith("/listings/")
                else {"fileSize": str(len(DATA))}
                if "/expansionFiles/" in suffix
                else {"bundles": []}
                if suffix == "/bundles"
                else {"apks": [] if state.method == EXTERNAL else [{"versionCode": 7}]}
            )
        binary = state.method in DOWNLOAD_METHODS and parsed.path == endpoint
        response = io.BytesIO(DATA if binary else json.dumps(result).encode())
        response.status = 200
        response.headers = (
            {"Content-Type": "application/octet-stream", "Content-Length": str(len(DATA))}
            if binary
            else {}
        )
        return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Mock(handlers=[], open=send))
    return calls


async def successful(mcp, name, arguments):
    result = await mcp.call_tool(name, arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def upload(network, mcp, http, state):
    transfer = await successful(
        mcp,
        "begin_upload_transfer",
        {
            "method": state.method,
            "parameters": state.parameters,
            "mime_type": state.mime,
            "size_bytes": len(DATA),
            "sha256": hashlib.sha256(DATA).hexdigest(),
            "body": state.body,
        },
    )
    handle = transfer["transfer_id"]
    response = await http.put(
        network.settings.issuer + f"/transfers/{handle}/content",
        content=DATA,
        headers={"Content-Type": state.mime},
    )
    assert response.status_code == 200, response.text
    status = await successful(mcp, "get_transfer_status", {"transfer_id": handle})
    assert status["state"] == "READY"
    return handle


async def prepare(mcp, state, handle=None):
    if state.kind == "internal_sharing_upload":
        arguments = {
            "method": state.method,
            "package_name": APP,
            "upload_handle": handle,
            "acknowledge_sharing_effects": True,
        }
    else:
        arguments = {
            "method": state.method,
            "parameters": state.parameters,
            "body": state.body,
            "upload_handle": handle,
            "mime_type": state.mime,
            "acknowledge_live_effects"
            if state.kind == "appstore_operation"
            else "acknowledge_effects": True,
        }
    return await successful(mcp, "prepare_" + state.kind, arguments)


@pytest.mark.parametrize("method", METHODS)
def test_all_eighteen_methods_over_real_tls_mcp_and_byte_routes(loopback, monkeypatch, method):
    _, token = connect(loopback)
    state = fixture_case(method)
    calls = google(monkeypatch, state)

    async def exercise():
        async with client(loopback, token) as (mcp, http):
            assert len((await mcp.list_tools()).tools) == 46
            inventory = await successful(mcp, "list_api_methods", {"limit": 200})
            assert sum(m["execution"]["implemented"] for m in inventory["methods"]) == 170
            if method in READ_METHODS:
                result = await successful(
                    mcp, "query_appstore", {"method": method, "parameters": state.parameters}
                )
                assert result["data"] == state.result
            elif method in DOWNLOAD_METHODS:
                result = await successful(
                    mcp, "download_artifact", {"method": method, "parameters": state.parameters}
                )
                handle = result["transfer_id"]
                for _ in range(2):
                    response = await http.get(
                        loopback.settings.issuer + f"/transfers/{handle}/content"
                    )
                    assert response.status_code == 200 and response.content == DATA
                    assert response.headers["cache-control"] == "no-store"
                await successful(mcp, "discard_transfer", {"transfer_id": handle})
                assert len(calls) == 1
            else:
                handle = await upload(loopback, mcp, http, state) if state.mime else None
                assert not calls
                prepared = await prepare(mcp, state, handle)
                assert all(call[0] == "GET" for call in calls)
                operation = prepared["operation_id"]
                args = {"operation_id": operation, "confirmation": operation}
                applied = await successful(mcp, "apply_" + state.kind, args)
                assert applied["mutation_succeeded"]
                assert len([c for c in calls if c[0] == "POST"]) == 1
                count = len(calls)
                replay = await mcp.call_tool("apply_" + state.kind, args)
                assert replay.is_error and len(calls) == count
            assert not loopback.app.state.oauth.transfers._records

    asyncio.run(exercise())


def test_reference_helper_uses_actual_sdk_session_and_rotated_token_for_both_byte_routes(
    loopback, monkeypatch, tmp_path, caplog
):
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthToken

    from pubship.hosted_transfer_client import TransferClient, tool

    request, initial = connect(loopback)
    source = tmp_path / "client-owned-upload.apk"
    source.write_bytes(DATA)
    source.chmod(0o600)
    destination = tmp_path / "client-owned-download.apk"
    state = fixture_case(P + "edits.apks.upload")
    upload_calls = google(monkeypatch, state)
    requests = []

    class Storage:
        tokens = OAuthToken.model_validate(initial)
        registration = None
        rotations = 0

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens
            self.rotations += 1

        async def get_client_info(self):
            return self.registration

        async def set_client_info(self, registration):
            self.registration = registration

    async def exercise():
        storage = Storage()
        storage.registration = await loopback.app.state.oauth.get_client(request["client_id"])

        async def forbidden(*args):
            raise AssertionError("Existing SDK connection must not start new authorization")

        auth = OAuthClientProvider(
            loopback.settings.resource,
            storage.registration,
            storage,
            redirect_handler=forbidden,
            callback_handler=forbidden,
        )

        async def audit(request):
            requests.append((request.method, request.url.path))

        async with httpx2.AsyncClient(
            auth=auth,
            verify=loopback.tls,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"request": [audit]},
        ) as http:
            async with streamable_http_client(
                loopback.settings.resource, http_client=http
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    auth.context.token_expiry_time = 1
                    helper = TransferClient(
                        loopback.settings.issuer,
                        session,
                        storage,
                        transport=httpx2.AsyncHTTPTransport(verify=loopback.tls),
                    )
                    uploaded = await helper.upload(
                        source=source,
                        method=state.method,
                        parameters=state.parameters,
                        mime_type=state.mime,
                    )
                    assert uploaded["state"] == "READY"
                    assert storage.tokens.access_token != initial["access_token"]
                    assert storage.tokens.refresh_token != initial["refresh_token"]
                    assert requests.count(("POST", "/token")) == 1
                    assert loopback.byte_authorizations == [
                        ("PUT", ("Bearer " + storage.tokens.access_token).encode())
                    ]
                    # Preparation consumes this same connection's helper-created handle.
                    prepared = await tool(
                        session,
                        "prepare_artifact_upload",
                        {
                            "method": state.method,
                            "parameters": state.parameters,
                            "mime_type": state.mime,
                            "upload_handle": uploaded["transfer_id"],
                            "acknowledge_effects": True,
                        },
                    )
                    applied = await tool(
                        session,
                        "apply_artifact_upload",
                        {
                            "operation_id": prepared["operation_id"],
                            "confirmation": prepared["operation_id"],
                        },
                    )
                    assert applied["mutation_succeeded"]
                    assert len([c for c in upload_calls if c[0] == "POST"]) == 1
                    downloaded_state = fixture_case(P + "generatedapks.download")
                    download_calls = google(monkeypatch, downloaded_state)
                    ready = await tool(
                        session,
                        "download_artifact",
                        {
                            "method": downloaded_state.method,
                            "parameters": downloaded_state.parameters,
                        },
                    )
                    saved = await helper.download(
                        transfer_id=ready["transfer_id"], destination=destination
                    )
                    assert saved["state"] == "saved" and destination.read_bytes() == DATA
                    assert destination.stat().st_mode & 0o777 == 0o600
                    assert len(download_calls) == 1
                    assert loopback.byte_authorizations[-1] == (
                        "GET",
                        ("Bearer " + storage.tokens.access_token).encode(),
                    )
                    assert requests.count(("POST", "/token")) == 1
                    assert storage.rotations == 1
                    assert storage.tokens.access_token not in caplog.text
                    assert storage.tokens.refresh_token not in caplog.text
                    await tool(session, "discard_transfer", {"transfer_id": ready["transfer_id"]})
                    assert not loopback.app.state.oauth.transfers._records

    asyncio.run(exercise())
