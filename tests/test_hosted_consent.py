"""HTTP consent and token scope tests; Google responses remain synthetic."""

import asyncio
import base64
import hashlib
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from test_hosted import callback, exchange
from test_hosted import hosted as hosted

from pubship.hosted_grants import ALL_SCOPES, READ_SCOPE

APP = "com.example.one"
OTHER = "com.example.two"
LISTINGS = "play:edit-listings"
COMMIT = "play:edit-commit"


def request_consent(client, settings, scopes):
    redirect = "https://client.example/callback"
    registered = client.post(
        "/register",
        json={
            "client_name": "Synthetic capability client",
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": " ".join(sorted(ALL_SCOPES)),
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
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
            "scope": " ".join(scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "resource": settings.resource,
        },
    )
    assert response.status_code == 302, response.text
    connect = response.headers["location"]
    page = client.get(connect)
    assert page.status_code == 200, page.text
    return connect, page, {"client_id": client_id, "verifier": verifier, "redirect": redirect}


def choose(
    client,
    settings,
    connect,
    *,
    capabilities=(),
    fields=None,
    packages=APP,
    services=("publisher",),
):
    data = {
        "packages": packages,
        "services": list(services),
        "capabilities": list(capabilities),
    }
    data.update(
        {"capability_packages[" + name + "]": value for name, value in (fields or {}).items()}
    )
    return client.post(connect, data=data, headers={"Origin": settings.issuer})


def finish(client, settings, request, response):
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    request["state"] = query["state"][0]
    code = callback(client, request, services=("publisher",))
    token = exchange(client, settings, request, code)
    assert token.status_code == 200, token.text
    return token.json()


def test_consent_only_displays_requested_capabilities_and_separate_package_fields(hosted):
    settings, _, client = hosted
    _, page, _ = request_consent(client, settings, [READ_SCOPE, LISTINGS])
    assert "value='play:edit-listings'" in page.text
    assert "capability_packages[play:edit-listings]" in page.text
    assert "value='play:edit-commit'" not in page.text
    assert "name='capabilities' value='play:edit-listings' checked" not in page.text


def test_declining_all_optional_capabilities_returns_read_only_token(hosted):
    settings, app, client = hosted
    connect, _, request = request_consent(client, settings, [READ_SCOPE, LISTINGS, COMMIT])
    token = finish(client, settings, request, choose(client, settings, connect))
    assert token["scope"] == READ_SCOPE
    principal = asyncio.run(app.state.oauth.load_access_token(token["access_token"]))
    assert set(app.state.oauth.authorize_current(principal).capability_packages) == {READ_SCOPE}


def test_selected_capability_and_package_narrowing_survive_oauth_and_refresh(hosted):
    settings, app, client = hosted
    connect, _, request = request_consent(client, settings, [READ_SCOPE, LISTINGS, COMMIT])
    response = choose(
        client,
        settings,
        connect,
        capabilities=[LISTINGS],
        fields={LISTINGS: APP, COMMIT: ""},
        packages=APP + "," + OTHER,
    )
    token = finish(client, settings, request, response)
    assert set(token["scope"].split()) == {READ_SCOPE, LISTINGS}
    principal = asyncio.run(app.state.oauth.load_access_token(token["access_token"]))
    authorization = app.state.oauth.authorize_current(principal)
    assert authorization.capability_packages[LISTINGS] == {APP}
    assert authorization.capability_packages[READ_SCOPE] == {APP, OTHER}
    refresh = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": request["client_id"],
            "refresh_token": token["refresh_token"],
            "scope": READ_SCOPE,
            "resource": settings.resource,
        },
    )
    assert refresh.status_code == 200, refresh.text
    assert refresh.json()["scope"] == READ_SCOPE
    denied = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": request["client_id"],
            "refresh_token": refresh.json()["refresh_token"],
            "scope": READ_SCOPE + " " + LISTINGS,
            "resource": settings.resource,
        },
    )
    assert denied.status_code == 400
    assert denied.json()["error"] == "invalid_scope"


@pytest.mark.parametrize(
    "capabilities,fields,services",
    [
        ([COMMIT], {COMMIT: APP}, ("publisher",)),
        ([LISTINGS], {LISTINGS: OTHER}, ("publisher",)),
        ([LISTINGS], {LISTINGS: APP}, ("reporting",)),
        ([LISTINGS], {}, ("publisher",)),
        ([], {LISTINGS: APP}, ("publisher",)),
        ([LISTINGS, LISTINGS], {LISTINGS: APP}, ("publisher",)),
    ],
)
def test_forged_consent_is_rejected_before_google_redirect(hosted, capabilities, fields, services):
    settings, _, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, LISTINGS])
    with patch("pubship.hosted.AsyncOAuth2Client.create_authorization_url") as google:
        response = choose(
            client, settings, connect, capabilities=capabilities, fields=fields, services=services
        )
    assert response.status_code == 400
    google.assert_not_called()


def test_unrequested_capability_cannot_be_granted_by_adding_form_fields(hosted):
    settings, _, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE])
    response = choose(client, settings, connect, capabilities=[LISTINGS], fields={LISTINGS: APP})
    assert response.status_code == 400


def test_duplicate_scalar_consent_fields_fail_closed(hosted):
    settings, _, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, LISTINGS])
    response = client.post(
        connect,
        content="packages=com.example.one&packages=com.example.two&services=publisher",
        headers={"Origin": settings.issuer, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400


def test_same_google_identity_two_connections_keep_distinct_capability_grants(hosted):
    settings, app, client = hosted
    connect, _, request = request_consent(client, settings, [READ_SCOPE, LISTINGS])
    first = finish(
        client,
        settings,
        request,
        choose(client, settings, connect, capabilities=[LISTINGS], fields={LISTINGS: APP}),
    )
    connect, _, request = request_consent(client, settings, [READ_SCOPE])
    second = finish(client, settings, request, choose(client, settings, connect))
    one = asyncio.run(app.state.oauth.load_access_token(first["access_token"]))
    two = asyncio.run(app.state.oauth.load_access_token(second["access_token"]))
    assert one.subject != two.subject and one.client_id != two.client_id
    assert LISTINGS in app.state.oauth.authorize_current(one).scopes
    assert LISTINGS not in app.state.oauth.authorize_current(two).scopes
    app.state.oauth.disconnect(one.subject)
    assert asyncio.run(app.state.oauth.load_access_token(first["access_token"])) is None
    assert asyncio.run(app.state.oauth.load_access_token(second["access_token"])) is not None
