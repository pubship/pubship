"""Store-only OAuth completes through the HTTP consent and token endpoints."""

import asyncio

import pytest
from test_hosted import hosted as hosted
from test_hosted_consent import finish, request_consent

from pubship.errors import PlayError
from pubship.hosted_grants import APPSTORE_EVENTS, APPSTORE_PAIR_SCOPES, READ_SCOPE

STORE = "com.example.store"
APP = "com.example.app"


@pytest.mark.parametrize("capability", sorted(APPSTORE_PAIR_SCOPES | {APPSTORE_EVENTS}))
def test_store_only_browser_consent_and_token_refresh_keep_dimensions(hosted, capability):
    settings, app, client = hosted
    connect, page, request = request_consent(client, settings, [READ_SCOPE, capability])
    assert f"appstore_choices[{capability}]" in page.text
    assert f"capability_packages[{capability}]" not in page.text
    assert f"value='{capability}' checked" not in page.text
    response = client.post(
        connect,
        data={
            "packages": "",
            "services": "publisher",
            "capabilities": capability,
            f"appstore_choices[{capability}]": STORE
            if capability == APPSTORE_EVENTS
            else f"{STORE}/{APP}",
        },
        headers={"Origin": settings.issuer},
    )
    token = finish(client, settings, request, response)
    assert set(token["scope"].split()) == {READ_SCOPE, capability}
    for phase in range(2):
        principal = asyncio.run(app.state.oauth.load_access_token(token["access_token"]))
        auth = app.state.oauth.authorize_current(principal)
        assert auth.authorization_schema_version == 3
        assert not auth.capability_packages[READ_SCOPE]
        target = None if capability == APPSTORE_EVENTS else APP
        app.state.oauth.authorize_appstore_current(principal, capability, STORE, target)
        with pytest.raises(PlayError):
            app.state.oauth.authorize_current(principal, READ_SCOPE, APP)
        if phase == 0:
            refreshed = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": request["client_id"],
                    "refresh_token": token["refresh_token"],
                    "resource": settings.resource,
                },
            )
            assert refreshed.status_code == 200, refreshed.text
            token = refreshed.json()


@pytest.mark.parametrize(
    "extra",
    [
        {"capabilities": "play:appstore-catalog"},
        {"appstore_choices[play:appstore-catalog]": f"{STORE}/{APP}"},
        {"capability_packages[play:appstore-events]": STORE},
        {
            "capabilities": APPSTORE_EVENTS,
            "appstore_choices[play:appstore-events]": f"{STORE}/{APP}",
        },
    ],
)
def test_forged_or_wrong_dimension_forms_fail_before_google(hosted, extra):
    settings, _, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, APPSTORE_EVENTS])
    data = {"packages": "", "services": "publisher", **extra}
    response = client.post(connect, data=data, headers={"Origin": settings.issuer})
    assert response.status_code == 400
    assert "location" not in response.headers
