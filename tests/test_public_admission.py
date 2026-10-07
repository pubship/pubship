"""Synthetic admission boundaries: no live Google or public service requests."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

import anyio
import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AuthorizeError, RegistrationError
from starlette.testclient import TestClient
from test_hosted import begin, call_tool, connect_account
from test_hosted import hosted as hosted  # noqa: F401

from pubship import hosted as module
from pubship.errors import PlayError
from pubship.hosted import RequestLimits, create_hosted_app
from pubship.hosted_config import HostedSettings
from pubship.hosted_pages import SECURE_HEADERS

RESOURCE = "https://mcp.example.test/mcp"


def scope(path="/register", **kwargs):
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "client": ("127.0.0.1", 12345),
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        **kwargs,
    }


async def accepted(scope, receive, send):
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def invoke(app, request_scope, receive=None):
    events = []

    async def empty():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        events.append(event)

    await app(request_scope, receive or empty, send)
    return events


def assert_secure_error(events, status, error):
    assert events[0]["status"] == status
    headers = dict(events[0]["headers"])
    for key, value in SECURE_HEADERS.items():
        assert headers[key.lower().encode()] == value.encode()
    assert json.loads(events[1]["body"]) == {"error": error}


@pytest.mark.parametrize("path", ["/mcp", "/register", "/token", "/revoke", "/connect"])
def test_idle_body_timeout_before_dispatch(monkeypatch, path):
    monkeypatch.setattr(module, "BODY_IDLE_TIMEOUT", 0.02)
    monkeypatch.setattr(module, "BODY_TOTAL_TIMEOUT", 0.5)
    dispatched = AsyncMock()

    async def stalled():
        await anyio.sleep_forever()

    events = asyncio.run(invoke(RequestLimits(dispatched, RESOURCE), scope(path), stalled))
    assert_secure_error(events, 408, "request_timeout")
    dispatched.assert_not_called()


def test_total_deadline_bounds_continuously_progressing_body(monkeypatch):
    monkeypatch.setattr(module, "BODY_IDLE_TIMEOUT", 0.1)
    monkeypatch.setattr(module, "BODY_TOTAL_TIMEOUT", 0.2)
    dispatched = AsyncMock()
    chunks = []

    async def trickle():
        await anyio.sleep(0.025)
        chunks.append(True)
        return {"type": "http.request", "body": b"x", "more_body": True}

    events = asyncio.run(invoke(RequestLimits(dispatched, RESOURCE), scope("/mcp"), trickle))
    assert len(chunks) >= 2
    assert_secure_error(events, 408, "request_timeout")
    dispatched.assert_not_called()


def test_body_deadline_does_not_cancel_tool_response_generation(monkeypatch):
    monkeypatch.setattr(module, "BODY_IDLE_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "BODY_TOTAL_TIMEOUT", 0.02)
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            await anyio.sleep_forever()
        delivered = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def slow_tool(scope, receive, send):
        assert (await receive())["body"] == b"{}"
        await anyio.sleep(0.06)
        await accepted(scope, receive, send)

    events = asyncio.run(invoke(RequestLimits(slow_tool, RESOURCE), scope("/mcp"), receive))
    assert events[0]["status"] == 204


@pytest.mark.parametrize("length, status", [(256 * 1024, 204), (256 * 1024 + 1, 413)])
def test_json_size_boundary_preserved(length, status):
    async def receive():
        return {"type": "http.request", "body": b"x" * length, "more_body": False}

    events = asyncio.run(invoke(RequestLimits(accepted, RESOURCE), scope(), receive))
    assert events[0]["status"] == status
    if status == 413:
        assert_secure_error(events, 413, "request_too_large")


def test_transfer_route_still_authenticates_before_consuming_body():
    async def forbidden_receive():
        raise AssertionError("Transfer authentication must precede body reads")

    events = asyncio.run(
        invoke(
            RequestLimits(accepted, RESOURCE),
            scope("/transfers/" + "a" * 43 + "/content", method="PUT"),
            forbidden_receive,
        )
    )
    assert events[0]["status"] == 204


def test_disconnect_during_body_never_dispatches():
    dispatched = AsyncMock()

    async def disconnected():
        return {"type": "http.disconnect"}

    assert not asyncio.run(invoke(RequestLimits(dispatched, RESOURCE), scope(), disconnected))
    dispatched.assert_not_called()


def test_admission_budgets_isolate_onboarding_token_and_revoke_and_ignore_forwarded_ip():
    app = RequestLimits(accepted, RESOURCE)

    async def scenario():
        for index in range(120):
            request_scope = scope(
                "/register",
                headers=[(b"x-forwarded-for", f"192.0.2.{index}".encode())],
            )
            assert (await invoke(app, request_scope))[0]["status"] == 204
        limited = await invoke(app, scope("/authorize"))
        assert_secure_error(limited, 429, "rate_limited")
        assert dict(limited[0]["headers"])[b"retry-after"] == b"60"
        for _ in range(120):
            assert (await invoke(app, scope("/token")))[0]["status"] == 204
        assert_secure_error(await invoke(app, scope("/token")), 429, "rate_limited")
        assert (await invoke(app, scope("/revoke")))[0]["status"] == 204
        assert (await invoke(app, scope("/register", client=("192.0.2.200", 1))))[0][
            "status"
        ] == 204

    asyncio.run(scenario())


def test_closed_by_default_and_routes_reject_before_body_or_provider_work(tmp_path):
    tmp_path.chmod(0o700)
    settings = HostedSettings(
        "https://mcp.example.test",
        "synthetic",
        "synthetic",
        tmp_path / "vault.db",
        Fernet.generate_key(),
        allowed_emails=frozenset({"owner@example.test"}),
    )
    assert settings.new_enrollment_enabled is False
    app = create_hosted_app(settings)
    try:
        with TestClient(app, base_url=settings.issuer, follow_redirects=False) as client:
            with patch.object(module.AsyncOAuth2Client, "fetch_token", new=AsyncMock()) as fetch:
                for path in module.ENROLLMENT_PATHS:
                    for suffix in ("", "/"):
                        response = client.post(path + suffix, content=b"x" * (256 * 1024 + 1))
                        assert response.status_code == 403
                        assert response.json() == {"error": "enrollment_closed"}
                        assert "location" not in response.headers
                        assert response.headers["cache-control"] == "no-store"
                fetch.assert_not_called()
            assert client.get("/healthz").status_code == 200
            assert client.get("/.well-known/oauth-authorization-server").status_code == 200
            assert client.post("/mcp", json={}).status_code == 401
    finally:
        app.state.vault.close()


def test_provider_rejects_direct_enrollment_when_closed(hosted):
    _, app, _ = hosted
    provider = app.state.oauth
    provider.new_enrollment_enabled = False
    with pytest.raises(RegistrationError) as registration:
        asyncio.run(provider.register_client(None))
    assert "enrollment" in registration.value.error_description
    with pytest.raises(AuthorizeError) as authorization:
        asyncio.run(provider.authorize(None, None))
    assert "enrollment" in authorization.value.error_description
    with pytest.raises(PlayError, match="enrollment"):
        provider.complete_google({}, {})
    with pytest.raises(PlayError, match="explicitly"):
        provider.new_enrollment_enabled = "false"
    assert provider.new_enrollment_enabled is False


def test_pending_callback_and_consent_reject_after_closure(hosted):
    settings, app, client = hosted
    request = begin(client, settings)
    app.state.oauth.new_enrollment_enabled = False
    with (
        patch.object(module.AsyncOAuth2Client, "fetch_token", new=AsyncMock()) as fetch,
        patch.object(app.state.vault, "put_many") as persist,
    ):
        response = client.get(
            "/google/callback", params={"state": request["state"], "code": "synthetic"}
        )
        assert response.status_code == 403
        assert client.get("/connect", params={"request": "a" * 43}).status_code == 403
        fetch.assert_not_called()
        persist.assert_not_called()


def test_gate_closure_during_google_exchange_prevents_grant(hosted, monkeypatch):
    settings, app, client = hosted
    request = begin(client, settings)

    async def closing_exchange(*args, **kwargs):
        app.state.oauth.new_enrollment_enabled = False
        return {}

    monkeypatch.setattr(module.AsyncOAuth2Client, "fetch_token", closing_exchange)
    persist = Mock(side_effect=AssertionError("Closed enrollment must not persist a grant"))
    monkeypatch.setattr(app.state.vault, "put_many", persist)
    response = client.get(
        "/google/callback", params={"state": request["state"], "code": "synthetic"}
    )
    assert response.status_code == 400
    assert "location" not in response.headers
    assert app.state.vault.get("google_state", request["state"]) is None
    persist.assert_not_called()


def test_existing_customer_reads_refresh_and_revocation_survive_closure(hosted):
    settings, app, client = hosted
    request, tokens = connect_account(client, settings, "com.example.alice", "synthetic-google")
    app.state.oauth.new_enrollment_enabled = False
    with patch("pubship.http.get", return_value={"reviews": []}):
        response = call_tool(
            client, tokens["access_token"], "list_reviews", {"package": "com.example.alice"}
        )
    assert response.status_code == 200
    assert not response.json()["result"].get("isError")
    response = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": request["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": settings.resource,
        },
    )
    assert response.status_code == 200
    refreshed = response.json()
    response = client.post(
        "/revoke", data={"client_id": request["client_id"], "token": refreshed["refresh_token"]}
    )
    assert response.status_code == 200
    assert (
        call_tool(
            client, refreshed["access_token"], "list_reviews", {"package": "com.example.alice"}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("value", ["yes", "1", "TRUE", "", "false "])
def test_environment_enrollment_requires_explicit_boolean(hosted, monkeypatch, value):
    settings, _, _ = hosted
    for name in (
        "PUBSHIP_SERVER_ISSUER",
        "PUBSHIP_SERVER_GOOGLE_CLIENT_FILE",
        "PUBSHIP_SERVER_VAULT_KEY_FILE",
        "PUBSHIP_SERVER_VAULT_DATABASE",
        "PUBSHIP_SERVER_TRANSFER_DIRECTORY",
    ):
        monkeypatch.setenv(name, settings.issuer)
    monkeypatch.setenv("PUBSHIP_SERVER_ALLOWED_EMAILS", "owner@example.test")
    monkeypatch.setenv("PUBSHIP_SERVER_NEW_ENROLLMENT_ENABLED", value)
    with pytest.raises(PlayError, match="must be true or false"):
        HostedSettings.from_env()
    with pytest.raises(PlayError, match="explicitly"):
        replace(settings, new_enrollment_enabled=value)


@pytest.mark.parametrize("value, enabled", [(None, False), ("false", False), ("true", True)])
def test_environment_enrollment_default_and_explicit_opt_in(tmp_path, monkeypatch, value, enabled):
    from pubship import hosted_config

    issuer = "https://mcp.example.test"
    client_path = tmp_path / "client.json"
    key_path = tmp_path / "key"
    client_data = json.dumps(
        {
            "web": {
                "client_id": "synthetic",
                "client_secret": "synthetic",
                "redirect_uris": [issuer + "/google/callback"],
            }
        }
    ).encode()
    key = Fernet.generate_key()
    monkeypatch.setattr(
        hosted_config, "private_file", lambda path: client_data if path == client_path else key
    )
    for name, setting in {
        "PUBSHIP_SERVER_ALLOWED_EMAILS": "owner@example.test",
        "PUBSHIP_SERVER_ISSUER": issuer,
        "PUBSHIP_SERVER_GOOGLE_CLIENT_FILE": str(client_path),
        "PUBSHIP_SERVER_VAULT_KEY_FILE": str(key_path),
        "PUBSHIP_SERVER_VAULT_DATABASE": str(tmp_path / "vault.db"),
        "PUBSHIP_SERVER_TRANSFER_DIRECTORY": str(tmp_path / "transfers"),
    }.items():
        monkeypatch.setenv(name, setting)
    if value is None:
        monkeypatch.delenv("PUBSHIP_SERVER_NEW_ENROLLMENT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PUBSHIP_SERVER_NEW_ENROLLMENT_ENABLED", value)
    assert HostedSettings.from_env().new_enrollment_enabled is enabled


def test_service_recovers_after_body_timeout(monkeypatch):
    monkeypatch.setattr(module, "BODY_IDLE_TIMEOUT", 0.01)
    app = RequestLimits(accepted, RESOURCE)

    async def stalled():
        await anyio.sleep_forever()

    async def scenario():
        assert_secure_error(await invoke(app, scope(), stalled), 408, "request_timeout")
        assert (await invoke(app, scope()))[0]["status"] == 204

    asyncio.run(scenario())
