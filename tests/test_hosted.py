import asyncio
import base64
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from pubship.auth import SCOPES
from pubship.errors import PlayError
from pubship.hosted import create_hosted_app
from pubship.hosted_config import HostedSettings
from pubship.oauth import READ_SCOPE, CustomerAuth
from pubship.vault import Vault, private_file


@pytest.fixture
def hosted(tmp_path, synthetic_google_identity):
    tmp_path.chmod(0o700)
    settings = HostedSettings(
        "https://mcp.example.test",
        "synthetic-client",
        "PRIVATE-CLIENT-SECRET",
        tmp_path / "vault.db",
        Fernet.generate_key(),
        allowed_emails=frozenset({"owner@example.test"}),
        new_enrollment_enabled=True,
    )
    with patch(
        "pubship.server.Auth",
        side_effect=AssertionError("Hosted server must never construct local Auth"),
    ):
        app = create_hosted_app(settings)
    with TestClient(app, base_url=settings.issuer, follow_redirects=False) as client:
        yield settings, app, client
    app.state.vault.close()


def begin(client, settings, package="com.example.alice", services=("publisher", "reporting")):
    redirect = "https://client.example/callback"
    response = client.post(
        "/register",
        json={
            "client_name": "Example MCP client",
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": READ_SCOPE,
        },
    )
    assert response.status_code == 201, response.text
    client_id = response.json()["client_id"]
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    response = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": READ_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "resource": settings.resource,
        },
    )
    assert response.status_code == 302, response.text
    connect_url = response.headers["location"]
    response = client.get(connect_url)
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
    response = client.post(
        connect_url,
        data={"packages": package, "services": list(services)},
        headers={"Origin": settings.issuer},
    )
    assert response.status_code == 303, response.text
    google_url = urlsplit(response.headers["location"])
    assert google_url.netloc == "accounts.google.com"
    query = parse_qs(google_url.query)
    assert query["code_challenge_method"] == ["S256"]
    assert "PRIVATE-CLIENT-SECRET" not in response.headers["location"]
    return {
        "client_id": client_id,
        "verifier": verifier,
        "redirect": redirect,
        "state": query["state"][0],
    }


def callback(client, request, marker="PRIVATE-GOOGLE-ALICE", services=("publisher", "reporting")):
    token = {
        "access_token": marker,
        "refresh_token": marker + "-REFRESH",
        "scope": " ".join(SCOPES[s] for s in services),
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    with patch(
        "pubship.hosted.AsyncOAuth2Client.fetch_token", new=AsyncMock(return_value=token)
    ) as fetch:
        response = client.get(
            "/google/callback", params={"state": request["state"], "code": "PRIVATE-GOOGLE-CODE"}
        )
    assert response.status_code == 303, response.text
    assert fetch.call_args.kwargs["code_verifier"]
    target = urlsplit(response.headers["location"])
    assert target.netloc == "client.example"
    params = parse_qs(target.query)
    assert params["state"] == ["client-state"]
    assert "PRIVATE-GOOGLE" not in response.text + response.headers["location"]
    return params["code"][0]


def exchange(client, settings, request, code, verifier=None):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": request["client_id"],
            "redirect_uri": request["redirect"],
            "code": code,
            "code_verifier": verifier or request["verifier"],
            "resource": settings.resource,
        },
    )


def connect_account(client, settings, package, marker):
    request = begin(client, settings, package)
    code = callback(client, request, marker)
    response = exchange(client, settings, request, code)
    assert response.status_code == 200, response.text
    assert "PRIVATE-GOOGLE" not in response.text
    return request, response.json()


def call_tool(client, token, name, arguments):
    return client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def test_metadata_no_credentials_required_and_protected_mcp(hosted):
    settings, _, client = hosted
    resource = client.get("/.well-known/oauth-protected-resource/mcp")
    assert resource.status_code == 200
    assert resource.json()["resource"] == settings.resource
    assert client.get("/.well-known/oauth-authorization-server").status_code == 200
    response = client.post("/mcp", json={})
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]
    assert call_tool(client, "PRIVATE-GOOGLE-TOKEN", "list_api_methods", {}).status_code == 401


def test_two_customers_are_isolated_through_http_mcp(hosted, caplog):
    settings, _, client = hosted
    _, alice = connect_account(client, settings, "com.example.alice", "PRIVATE-GOOGLE-ALICE")
    _, bob = connect_account(client, settings, "com.example.bob", "PRIVATE-GOOGLE-BOB")
    calls = []

    def provider_read(url, token):
        calls.append((url, token))
        owner = "alice" if token == "PRIVATE-GOOGLE-ALICE" else "bob"
        assert "com.example." + owner in url
        return {"reviews": [{"reviewId": owner}]}

    with (
        patch("google.auth.default", side_effect=AssertionError("ADC forbidden")),
        patch("subprocess.run", side_effect=AssertionError("gcloud forbidden")),
        patch("pubship.http.get", side_effect=provider_read),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    call_tool,
                    client,
                    token["access_token"],
                    "list_reviews",
                    {"package": "com.example." + owner},
                )
                for owner, token in [("alice", alice), ("bob", bob)]
            ]
            responses = [future.result() for future in futures]
        assert all(response.status_code == 200 for response in responses)
        for response, owner in zip(responses, ["alice", "bob"], strict=True):
            assert (
                response.json()["result"]["structuredContent"]["data"]["reviews"][0]["reviewId"]
                == owner
            )
            assert "PRIVATE-GOOGLE" not in response.text
        denied = call_tool(
            client, alice["access_token"], "list_reviews", {"package": "com.example.bob"}
        )
        assert denied.json()["result"]["isError"]
    assert len(calls) == 2
    assert "PRIVATE-GOOGLE" not in caplog.text
    assert "PRIVATE-CLIENT-SECRET" not in caplog.text


def test_pkce_and_code_replay(hosted):
    settings, _, client = hosted
    request = begin(client, settings)
    code = callback(client, request)
    assert exchange(client, settings, request, code, "x" * 64).status_code == 400
    assert exchange(client, settings, request, code).status_code == 200
    assert exchange(client, settings, request, code).status_code == 400


def test_refresh_rotation_replay_revokes_connection(hosted):
    settings, _, client = hosted
    request, tokens = connect_account(client, settings, "com.example.alice", "PRIVATE-GOOGLE-ALICE")
    data = {
        "grant_type": "refresh_token",
        "client_id": request["client_id"],
        "refresh_token": tokens["refresh_token"],
        "resource": settings.resource,
    }
    refreshed = client.post("/token", data=data)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["refresh_token"] != tokens["refresh_token"]
    assert client.post("/token", data=data).status_code == 400
    assert (
        call_tool(client, refreshed.json()["access_token"], "list_api_methods", {}).status_code
        == 401
    )


def test_disconnect_only_revokes_current_customer(hosted):
    settings, _, client = hosted
    _, alice = connect_account(client, settings, "com.example.alice", "PRIVATE-GOOGLE-ALICE")
    _, bob = connect_account(client, settings, "com.example.bob", "PRIVATE-GOOGLE-BOB")
    response = call_tool(client, alice["access_token"], "disconnect_google_account", {})
    assert response.status_code == 200
    assert not response.json()["result"].get("isError")
    assert call_tool(client, alice["access_token"], "list_api_methods", {}).status_code == 401
    assert (
        call_tool(client, bob["access_token"], "list_api_methods", {"limit": 1}).status_code == 200
    )


def test_callback_requires_browser_binding_and_rejects_replay(hosted):
    settings, _, client = hosted
    request = begin(client, settings)
    saved = client.cookies.get("__Host-pubship-consent")
    client.cookies.clear()
    with patch("pubship.hosted.AsyncOAuth2Client.fetch_token", new=AsyncMock()) as fetch:
        response = client.get(
            "/google/callback", params={"state": request["state"], "code": "PRIVATE-GOOGLE-CODE"}
        )
    assert response.status_code == 400
    fetch.assert_not_called()
    client.cookies.set("__Host-pubship-consent", saved, domain="mcp.example.test", path="/")
    callback(client, request)
    with patch("pubship.hosted.AsyncOAuth2Client.fetch_token", new=AsyncMock()) as fetch:
        response = client.get(
            "/google/callback", params={"state": request["state"], "code": "PRIVATE-GOOGLE-CODE"}
        )
    assert response.status_code == 400
    fetch.assert_not_called()


@pytest.mark.parametrize(
    "redirect",
    [
        "http://evil.example/callback",
        "https://user:password@evil.example/callback",
        "https://client.example/#fragment",
        "file:///tmp/callback",
    ],
)
def test_registration_rejects_unsafe_redirects(hosted, redirect):
    _, _, client = hosted
    response = client.post(
        "/register", json={"redirect_uris": [redirect], "token_endpoint_auth_method": "none"}
    )
    assert response.status_code == 400


def test_hosted_rejects_wrong_host_and_oversize_requests(hosted):
    _, _, client = hosted
    assert client.get("/healthz", headers={"host": "evil.example"}).status_code == 400
    assert client.post("/register", content=b"x" * (256 * 1024 + 1)).status_code == 413


def test_no_environment_credential_fallback(hosted, monkeypatch):
    _, app, _ = hosted
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/must/not/read.json")
    monkeypatch.setenv("GOOGLE_PLAY_AUTH_MODE", "gcloud")
    with patch("google.auth.default") as adc, patch("subprocess.run") as gcloud:
        with pytest.raises(PlayError):
            app.state.oauth.services(None)
        fake = AccessToken(
            token="synthetic",
            client_id="fake",
            scopes=[READ_SCOPE],
            subject="fake",
            resource="https://wrong.example/mcp",
        )
        with pytest.raises(PlayError):
            app.state.oauth.services(fake)
    adc.assert_not_called()
    gcloud.assert_not_called()


def test_customer_google_refresh_failure_does_not_fall_back_or_leak(hosted):
    settings, app, client = hosted
    _, tokens = connect_account(client, settings, "com.example.alice", "PRIVATE-GOOGLE-ALICE")
    principal = asyncio.run(app.state.oauth.load_access_token(tokens["access_token"]))
    grant = app.state.vault.get("grants", principal.subject)
    grant["google_expires"] = time.time() - 1
    app.state.vault.put("grants", principal.subject, grant, 600)
    with (
        patch(
            "pubship.oauth.Credentials.refresh",
            side_effect=RuntimeError("PRIVATE-REFRESH-DETAIL"),
        ),
        patch("google.auth.default") as adc,
    ):
        with pytest.raises(PlayError) as error:
            CustomerAuth(app.state.oauth, principal.subject).token("publisher")
    adc.assert_not_called()
    assert "PRIVATE" not in str(error.value)


def test_vault_encrypts_secrets_and_survives_restart(tmp_path):
    tmp_path.chmod(0o700)
    key = Fernet.generate_key()
    path = tmp_path / "vault.db"
    vault = Vault(path, key)
    vault.put("grants", "PRIVATE-IDENTIFIER", {"token": "PRIVATE-GOOGLE-TOKEN"}, 600)
    assert vault.get("grants", "PRIVATE-IDENTIFIER")["token"] == "PRIVATE-GOOGLE-TOKEN"
    for file in tmp_path.iterdir():
        assert b"PRIVATE" not in file.read_bytes()
        assert file.stat().st_mode & 0o077 == 0
    vault.close()
    restarted = Vault(path, key)
    assert restarted.get("grants", "PRIVATE-IDENTIFIER")["token"] == "PRIVATE-GOOGLE-TOKEN"
    assert restarted.get("grants", "another-customer") is None
    assert restarted.get("grants", "PRIVATE-IDENTIFIER", consume=True)
    assert restarted.get("grants", "PRIVATE-IDENTIFIER", consume=True) is None
    restarted.close()


def test_vault_cannot_rebind_ciphertext_to_another_customer(tmp_path):
    tmp_path.chmod(0o700)
    vault = Vault(tmp_path / "vault.db", Fernet.generate_key())
    vault.put("grants", "alice", {"token": "PRIVATE-ALICE"}, 600)
    with vault.db:
        vault.db.execute(
            "UPDATE records SET key=? WHERE key=?", (vault.digest("bob"), vault.digest("alice"))
        )
    assert vault.get("grants", "bob") is None
    vault.close()


def test_secret_files_require_private_permissions_and_reject_symlinks(tmp_path):
    private = tmp_path / "secret"
    private.write_bytes(b"synthetic")
    private.chmod(0o644)
    with pytest.raises(PlayError):
        private_file(private)
    private.chmod(0o600)
    assert private_file(private) == b"synthetic"
    link = tmp_path / "link"
    link.symlink_to(private)
    with pytest.raises(PlayError):
        private_file(link)


def test_hosted_configuration_cannot_use_default_credentials(monkeypatch):
    for name in list(os.environ):
        if name.startswith("PUBSHIP_SERVER_"):
            monkeypatch.delenv(name)
    with pytest.raises(PlayError, match="Local ADC and gcloud are never used"):
        HostedSettings.from_env()


def test_hosted_config_repr_hides_key_and_secret(tmp_path):
    settings = HostedSettings(
        "https://mcp.example.test",
        "example-client",
        "PRIVATE-SECRET",
        tmp_path / "vault.db",
        b"PRIVATE-KEY",
        allowed_emails=frozenset({"owner@example.test"}),
    )
    assert "PRIVATE" not in repr(settings)
