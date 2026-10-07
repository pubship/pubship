"""Synthetic endpoint and concurrency regressions for hosted customer grants."""

import asyncio
import base64
import hashlib
import html
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_hosted import begin, call_tool, callback, connect_account, exchange
from test_hosted import hosted as hosted

from pubship.auth import SCOPES
from pubship.errors import PlayError
from pubship.oauth import CustomerAuth


def refresh_data(settings, request, tokens, **overrides):
    return {
        "grant_type": "refresh_token",
        "client_id": request["client_id"],
        "refresh_token": tokens["refresh_token"],
        "resource": settings.resource,
        **overrides,
    }


@pytest.mark.parametrize("resource", [None, "https://another.example/mcp"])
def test_authorize_requires_this_services_resource(hosted, resource):
    settings, _, client = hosted
    request = begin(client, settings)
    parameters = {
        "client_id": request["client_id"],
        "redirect_uri": request["redirect"],
        "response_type": "code",
        "scope": "play:read",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
        "state": "client-state",
    }
    if resource is not None:
        parameters["resource"] = resource
    response = client.get("/authorize", params=parameters)
    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    assert target.netloc == "client.example"
    assert parse_qs(target.query)["error"] == ["invalid_target"]


@pytest.mark.parametrize("grant_type", ["authorization_code", "refresh_token"])
def test_token_exchange_rejects_another_resource_without_consuming_grant(hosted, grant_type):
    settings, _, client = hosted
    request = begin(client, settings)
    code = callback(client, request)
    if grant_type == "authorization_code":
        data = {
            "grant_type": grant_type,
            "client_id": request["client_id"],
            "redirect_uri": request["redirect"],
            "code": code,
            "code_verifier": request["verifier"],
        }
    else:
        tokens = exchange(client, settings, request, code).json()
        data = refresh_data(settings, request, tokens)
    data["resource"] = "https://another.example/mcp"
    denied = client.post("/token", data=data)
    assert denied.status_code == 400, denied.text
    assert "access_token" not in denied.json()
    data["resource"] = settings.resource
    assert client.post("/token", data=data).status_code == 200


def test_refresh_scope_escalation_preserves_original_authorization(hosted):
    settings, _, client = hosted
    request, tokens = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    denied = client.post(
        "/token", data=refresh_data(settings, request, tokens, scope="play:read play:write")
    )
    assert denied.status_code == 400
    assert denied.json()["error"] == "invalid_scope"
    assert client.post("/token", data=refresh_data(settings, request, tokens)).status_code == 200


@pytest.mark.parametrize(
    "resource", ["", ["https://mcp.example.test/mcp", "https://another.example/mcp"]]
)
def test_empty_or_duplicate_token_resource_cannot_consume_refresh(hosted, resource):
    settings, _, client = hosted
    request, tokens = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    response = client.post(
        "/token", data=refresh_data(settings, request, tokens, resource=resource)
    )
    assert response.status_code == 400
    data = refresh_data(settings, request, tokens)
    data.pop("resource")
    # Omission inherits the resource already bound at authorization.
    assert client.post("/token", data=data).status_code == 200


def test_multipart_token_form_cannot_bypass_resource_binding(hosted):
    settings, _, client = hosted
    request, tokens = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    data = refresh_data(settings, request, tokens, resource="https://another.example/mcp")
    response = client.post("/token", files={key: (None, value) for key, value in data.items()})
    assert response.status_code in {400, 415}, response.text
    assert client.post("/token", data=refresh_data(settings, request, tokens)).status_code == 200


def test_ambiguous_content_type_cannot_bypass_token_resource_validation(hosted):
    settings, _, client = hosted
    request, tokens = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    fields = refresh_data(settings, request, tokens, resource="https://another.example/mcp")
    boundary = "synthetic-boundary"
    body = (
        "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            for key, value in fields.items()
        )
        + f"--{boundary}--\r\n"
    )
    response = client.post(
        "/token",
        content=body.encode(),
        headers=[
            ("Content-Type", f"multipart/form-data; boundary={boundary}"),
            ("Content-Type", "application/x-www-form-urlencoded"),
        ],
    )
    assert response.status_code in {400, 415}, response.text
    assert client.post("/token", data=refresh_data(settings, request, tokens)).status_code == 200


@pytest.mark.parametrize("scope", [None, "", SCOPES["publisher"]])
def test_partial_or_missing_google_scopes_cannot_mint_a_connection(hosted, scope):
    settings, _, client = hosted
    request = begin(client, settings)
    token = {"access_token": "PRIVATE-UPSTREAM", "expires_in": 3600, "token_type": "Bearer"}
    if scope is not None:
        token["scope"] = scope
    with patch(
        "pubship.hosted.AsyncOAuth2Client.fetch_token", new=AsyncMock(return_value=token)
    ) as fetch:
        response = client.get("/google/callback", params={"state": request["state"], "code": "x"})
        assert response.status_code == 400
        assert "location" not in response.headers
        assert "PRIVATE" not in response.text
        replay = client.get("/google/callback", params={"state": request["state"], "code": "x"})
    assert replay.status_code == 400
    assert fetch.await_count == 1


@pytest.mark.parametrize("parameters", [{"error": "access_denied"}, {}])
def test_google_denial_or_missing_code_consumes_state_without_provider_call(hosted, parameters):
    settings, _, client = hosted
    request = begin(client, settings)
    with patch("pubship.hosted.AsyncOAuth2Client.fetch_token", new=AsyncMock()) as fetch:
        response = client.get("/google/callback", params={"state": request["state"], **parameters})
        replay = client.get("/google/callback", params={"state": request["state"], "code": "x"})
    assert response.status_code == replay.status_code == 400
    fetch.assert_not_called()


def test_callback_provider_exception_is_redacted_and_not_replayable(hosted, caplog):
    settings, _, client = hosted
    request = begin(client, settings)
    with patch(
        "pubship.hosted.AsyncOAuth2Client.fetch_token",
        new=AsyncMock(side_effect=RuntimeError("PRIVATE-REFRESH-AND-CODE")),
    ) as fetch:
        response = client.get("/google/callback", params={"state": request["state"], "code": "x"})
        replay = client.get("/google/callback", params={"state": request["state"], "code": "x"})
    assert response.status_code == replay.status_code == 400
    assert fetch.await_count == 1
    assert "PRIVATE" not in response.text + caplog.text


def test_info_logging_does_not_expose_callback_codes_or_google_tokens(hosted, caplog):
    settings, _, client = hosted
    caplog.set_level(logging.INFO)
    request = begin(client, settings)
    callback(client, request, marker="PRIVATE-ACCESS-AND-REFRESH")
    assert "PRIVATE-ACCESS-AND-REFRESH" not in caplog.text
    assert "PRIVATE-GOOGLE-CODE" not in caplog.text
    assert request["state"] not in caplog.text


def test_consent_discloses_unverified_client_and_exact_escaped_destination(hosted):
    settings, _, client = hosted
    name = "<img src=x onerror=alert(1)>Official Google"
    redirect = "https://client.example/callback?next=%3Cscript%3E&safe=yes"
    registered = client.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
        },
    )
    assert registered.status_code == 201
    response = client.get(
        "/authorize",
        params={
            "client_id": registered.json()["client_id"],
            "redirect_uri": redirect,
            "response_type": "code",
            "resource": settings.resource,
            "code_challenge": "a" * 43,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 302
    consent = client.get(response.headers["location"])
    assert consent.status_code == 200
    assert html.escape(name) in consent.text
    assert "<img" not in consent.text
    assert html.escape(redirect) in consent.text
    assert "not verified" in consent.text
    assert consent.headers["cache-control"] == "no-store"


def test_extra_google_scopes_do_not_authorize_an_unselected_service(hosted):
    settings, _, client = hosted
    request = begin(client, settings, services=("publisher",))
    # Google may return previously granted scopes; the connection stays consent-bound.
    code = callback(client, request, services=("publisher", "reporting"))
    tokens = exchange(client, settings, request, code).json()
    with patch("pubship.http.get") as provider:
        result = call_tool(
            client,
            tokens["access_token"],
            "read_reporting",
            {"method": "playdeveloperreporting.apps.search", "parameters": {}},
        )
    assert result.status_code == 200
    assert result.json()["result"]["isError"]
    assert "not authorized" in result.text
    provider.assert_not_called()


def test_official_mcp_http_client_preserves_customer_scope_on_tool_calls(hosted):
    settings, app, browser = hosted
    _, tokens = connect_account(browser, settings, "com.example.alice", "PRIVATE-ALICE")

    async def exercise():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + tokens["access_token"]},
        ) as http_client:
            async with Client(
                streamable_http_client(settings.resource, http_client=http_client)
            ) as client:
                tools = await client.list_tools()
                assert "disconnect_google_account" in {tool.name for tool in tools.tools}
                result = await client.call_tool("list_reviews", {"package": "com.example.alice"})
                assert not result.is_error
                assert result.structured_content["data"]["reviews"] == [{"reviewId": "synthetic"}]
                denied = await client.call_tool("list_reviews", {"package": "com.example.bob"})
                assert denied.is_error

    with patch(
        "pubship.http.get", return_value={"reviews": [{"reviewId": "synthetic"}]}
    ) as provider:
        browser.portal.call(exercise)
    provider.assert_called_once()
    assert provider.call_args.args[1] == "PRIVATE-ALICE"


def test_another_client_cannot_redeem_or_revoke_a_customers_refresh_token(hosted):
    settings, _, client = hosted
    alice_request, alice = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    bob_request, bob = connect_account(client, settings, "com.example.bob", "PRIVATE-BOB")
    denied = client.post("/token", data=refresh_data(settings, bob_request, alice))
    assert denied.status_code == 400
    revoked = client.post(
        "/revoke",
        data={
            "client_id": bob_request["client_id"],
            "client_secret": "",
            "token": alice["access_token"],
        },
    )
    assert revoked.status_code == 200
    for tokens in (alice, bob):
        assert (
            call_tool(client, tokens["access_token"], "list_api_methods", {"limit": 1}).status_code
            == 200
        )
    assert (
        client.post("/token", data=refresh_data(settings, alice_request, alice)).status_code == 200
    )


def test_public_client_can_revoke_without_a_client_secret(hosted):
    settings, _, client = hosted
    request, tokens = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    response = client.post(
        "/revoke", data={"client_id": request["client_id"], "token": tokens["refresh_token"]}
    )
    assert response.status_code == 200, response.text
    assert call_tool(client, tokens["access_token"], "list_api_methods", {}).status_code == 401


def test_revocation_adapter_does_not_waive_confidential_client_authentication(hosted):
    _, _, client = hosted
    registration = client.post(
        "/register",
        json={
            "redirect_uris": ["https://client.example/callback"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert registration.status_code == 201
    registered = registration.json()
    data = {"client_id": registered["client_id"], "token": "synthetic-unknown-token"}
    assert client.post("/revoke", data=data).status_code == 401
    data["client_secret"] = registered["client_secret"]
    assert client.post("/revoke", data=data).status_code == 200


def test_disconnect_is_per_connection_even_when_the_same_mcp_client_owns_both(hosted):
    settings, _, client = hosted
    request, first = connect_account(client, settings, "com.example.alice", "PRIVATE-FIRST")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(request["verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    response = client.get(
        "/authorize",
        params={
            "client_id": request["client_id"],
            "redirect_uri": request["redirect"],
            "response_type": "code",
            "scope": "play:read",
            "resource": settings.resource,
            "state": "client-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    connect_url = response.headers["location"]
    assert client.get(connect_url).status_code == 200
    response = client.post(
        connect_url,
        data={"packages": "com.example.bob", "services": ["publisher", "reporting"]},
        headers={"Origin": settings.issuer},
    )
    assert response.status_code == 303
    second_request = {
        **request,
        "state": parse_qs(urlsplit(response.headers["location"]).query)["state"][0],
    }
    code = callback(client, second_request, marker="PRIVATE-SECOND")
    second = exchange(client, settings, second_request, code).json()
    response = call_tool(client, first["access_token"], "disconnect_google_account", {})
    assert not response.json()["result"].get("isError")
    assert call_tool(client, first["access_token"], "list_api_methods", {}).status_code == 401
    with patch("pubship.http.get", return_value={"reviews": []}) as provider:
        response = call_tool(
            client, second["access_token"], "list_reviews", {"package": "com.example.bob"}
        )
    assert not response.json()["result"].get("isError")
    assert provider.call_args.args[1] == "PRIVATE-SECOND"
    assert (
        client.post("/token", data=refresh_data(settings, second_request, second)).status_code
        == 200
    )


def expire_google_token(app, tokens):
    principal = asyncio.run(app.state.oauth.load_access_token(tokens["access_token"]))
    grant = app.state.vault.get("grants", principal.subject)
    grant["google_expires"] = time.time() - 1
    app.state.vault.put("grants", principal.subject, grant, 600)
    return principal.subject


def test_concurrent_google_refresh_is_single_flight_and_other_grants_keep_working(hosted):
    settings, app, client = hosted
    _, alice = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    _, bob = connect_account(client, settings, "com.example.bob", "PRIVATE-BOB")
    alice_id = expire_google_token(app, alice)
    bob_principal = asyncio.run(app.state.oauth.load_access_token(bob["access_token"]))
    started, release, second_started = Event(), Event(), Event()

    def refresh(credentials, request):
        started.set()
        assert release.wait(5), "test must release the synthetic provider"
        credentials.token = "PRIVATE-ROTATED"
        credentials.expiry = datetime.now(UTC) + timedelta(hours=1)

    def second_read():
        second_started.set()
        return CustomerAuth(app.state.oauth, alice_id).token("publisher")

    with patch("pubship.oauth.Credentials.refresh", autospec=True, side_effect=refresh) as upstream:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(CustomerAuth(app.state.oauth, alice_id).token, "publisher")
            try:
                assert started.wait(5)
                second = pool.submit(second_read)
                assert second_started.wait(5)
                independent = pool.submit(
                    CustomerAuth(app.state.oauth, bob_principal.subject).token, "publisher"
                )
                assert independent.result(timeout=3) == "PRIVATE-BOB"
            finally:
                release.set()
            assert first.result(timeout=5) == second.result(timeout=5) == "PRIVATE-ROTATED"
    assert upstream.call_count == 1


def test_disconnect_during_google_refresh_does_not_resurrect_grant(hosted):
    settings, app, client = hosted
    _, tokens = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    grant_id = expire_google_token(app, tokens)
    started, release, disconnect_started = Event(), Event(), Event()

    def refresh(credentials, request):
        started.set()
        assert release.wait(5)
        credentials.token = "PRIVATE-ROTATED"
        credentials.expiry = datetime.now(UTC) + timedelta(hours=1)

    def disconnect():
        disconnect_started.set()
        app.state.oauth.disconnect(grant_id)

    with patch("pubship.oauth.Credentials.refresh", autospec=True, side_effect=refresh):
        with ThreadPoolExecutor(max_workers=2) as pool:
            refreshing = pool.submit(CustomerAuth(app.state.oauth, grant_id).token, "publisher")
            try:
                assert started.wait(5)
                disconnecting = pool.submit(disconnect)
                assert disconnect_started.wait(5)
            finally:
                release.set()
            refreshing.result(timeout=5)
            disconnecting.result(timeout=5)
    assert call_tool(client, tokens["access_token"], "list_api_methods", {}).status_code == 401
    with pytest.raises(PlayError):
        CustomerAuth(app.state.oauth, grant_id).token("publisher")


def test_simultaneous_refresh_replay_revokes_only_that_connection(hosted):
    settings, _, client = hosted
    request, alice = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    _, bob = connect_account(client, settings, "com.example.bob", "PRIVATE-BOB")
    barrier = Barrier(2)

    def refresh():
        barrier.wait(timeout=5)
        return client.post("/token", data=refresh_data(settings, request, alice))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=10) for future in [pool.submit(refresh), pool.submit(refresh)]
        ]
    assert sum(response.status_code == 200 for response in results) <= 1
    assert any(response.status_code == 400 for response in results)
    for response in results:
        if response.status_code == 200:
            assert (
                call_tool(
                    client, response.json()["access_token"], "list_api_methods", {}
                ).status_code
                == 401
            )
    assert call_tool(client, alice["access_token"], "list_api_methods", {}).status_code == 401
    assert (
        call_tool(client, bob["access_token"], "list_api_methods", {"limit": 1}).status_code == 200
    )
