"""Native structured consent, reviewed dimensions, and no provider mutation."""

import asyncio
import copy
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from test_hosted import hosted as hosted
from test_hosted_consent import finish, request_consent

from pubship.hosted import _admin_choices
from pubship.hosted_grants import (
    ACCOUNT_DIRECTORY,
    ACCOUNT_USERS,
    ADMIN_SCOPES,
    READ_SCOPE,
    SIGNING_ENROLL,
    SIGNING_ROTATE,
)

DEV = "123456789"
USER = "test-user@example.com"
APP = "com.example.app"
KEY = "projects/synthetic/locations/global/keyRings/example/cryptoKeys/example/cryptoKeyVersions/1"


def fields(capability):
    data = {"packages": "", "services": "publisher", "capabilities": capability}
    prefix = capability.removeprefix("play:") + "[0]"
    if capability == ACCOUNT_DIRECTORY:
        data["directory"] = DEV
    elif capability in {SIGNING_ENROLL, SIGNING_ROTATE}:
        data.update({prefix + "[app]": APP, prefix + "[kms_version]": KEY})
    else:
        data.update({prefix + "[developer]": DEV, prefix + "[user]": USER})
        if capability == ACCOUNT_USERS:
            data[prefix + "[methods]"] = "androidpublisher.users.delete"
            data[prefix + "[affected_apps][0][app]"] = APP
        else:
            data[prefix + "[methods]"] = "androidpublisher.grants.delete"
            data[prefix + "[app]"] = APP
    return data


class HiddenForm(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "input" and attrs.get("type") == "hidden":
            self.fields.append((attrs["name"], attrs["value"]))


def hidden(html):
    result = {}
    for key, value in HiddenForm(html).fields:
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


@pytest.mark.parametrize("capability", sorted(ADMIN_SCOPES))
def test_review_then_google_preserves_exact_v3_dimensions_and_single_use(hosted, capability):
    settings, app, client = hosted
    connect, page, request = request_consent(client, settings, [READ_SCOPE, capability])
    assert "value='" + capability + "' checked" not in page.text
    assert "capability_packages[" + capability + "]" not in page.text
    data = fields(capability)
    response = client.post(connect, data=data, headers={"Origin": settings.issuer})
    assert response.status_code == 200
    assert "Review exact connection authority" in response.text
    assert "location" not in response.headers
    assert DEV in response.text if capability.startswith("play:account") else KEY in response.text
    submitted = hidden(response.text)
    response = client.post(connect, data=submitted, headers={"Origin": settings.issuer})
    token = finish(client, settings, request, response)
    principal = asyncio.run(app.state.oauth.load_access_token(token["access_token"]))
    auth = app.state.oauth.authorize_current(principal)
    assert auth.authorization_schema_version == 3
    assert auth.capability_packages[READ_SCOPE] == frozenset()
    assert set(token["scope"].split()) == {READ_SCOPE, capability}
    replay = client.post(connect, data=submitted, headers={"Origin": settings.issuer})
    assert replay.status_code == 400


def test_review_change_requires_new_review_and_old_confirmation_never_applies(hosted):
    settings, _, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, ACCOUNT_DIRECTORY])
    response = client.post(
        connect, data=fields(ACCOUNT_DIRECTORY), headers={"Origin": settings.issuer}
    )
    old = hidden(response.text)
    changed = {**old, "directory": "987654321"}
    response = client.post(connect, data=changed, headers={"Origin": settings.issuer})
    assert response.status_code == 200
    assert "Review exact connection authority" in response.text
    assert hidden(response.text)["authority_confirmation"] != old["authority_confirmation"]
    response = client.post(connect, data=old, headers={"Origin": settings.issuer})
    assert response.status_code == 200
    assert "location" not in response.headers


@pytest.mark.parametrize("failure", ["origin", "cookie", "expired", "other_request"])
def test_confirmation_remains_bound_to_browser_pending_and_expiry(hosted, failure):
    settings, app, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, ACCOUNT_DIRECTORY])
    response = client.post(
        connect, data=fields(ACCOUNT_DIRECTORY), headers={"Origin": settings.issuer}
    )
    data = hidden(response.text)
    origin = settings.issuer
    if failure == "origin":
        origin = "https://foreign.example"
    elif failure == "cookie":
        client.cookies.clear()
    elif failure == "expired":
        key = parse_qs(urlsplit(connect).query)["request"][0]
        pending = app.state.vault.get("consent", key)
        pending["consent_expires_at"] = 1
        app.state.vault.put("consent", key, pending, 600)
    else:
        connect, _, _ = request_consent(client, settings, [READ_SCOPE, ACCOUNT_DIRECTORY])
    response = client.post(connect, data=data, headers={"Origin": origin})
    assert response.status_code == (
        200 if failure == "other_request" else 400 if failure == "expired" else 403
    )
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "change",
    [
        {"account-users[0][developer]": "*"},
        {"account-users[0][methods]": ""},
        {"account-users[0][methods]": "androidpublisher.users.delete,"},
        {
            "account-users[0][methods]": "androidpublisher.users.delete,androidpublisher.users.delete"
        },
        {"account-users[0][developer_permissions]": "ADMIN_EVERYTHING"},
        {"account-users[0][allow_expiration_change]": "true"},
        {"account-users[0][affected_apps][1][app]": APP},
        {"account-users[0][affected_apps][1][permissions]": "CAN_VIEW_NON_FINANCIAL_DATA"},
        {"account-users[3][developer]": DEV},
        {"directory": DEV},
        {"account_directory_developers": '["123"]'},
        {"capabilities": "play:signing-enroll"},
        {"account-users[0][user]": "x<script>@example.com"},
    ],
)
def test_malformed_or_unrequested_native_fields_fail_before_google(hosted, change):
    settings, _, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, ACCOUNT_USERS])
    response = client.post(
        connect, data={**fields(ACCOUNT_USERS), **change}, headers={"Origin": settings.issuer}
    )
    assert response.status_code == 400
    assert "location" not in response.headers
    assert "<script>" not in response.text


def test_rows_keep_empty_ceilings_and_exact_pair_identity():
    data = fields(ACCOUNT_USERS)
    data["account-users[1][developer]"] = "999"
    data["account-users[1][user]"] = "other@example.com"
    data["account-users[1][methods]"] = "androidpublisher.users.patch"
    data["account-users[1][allow_expiration_change]"] = "yes"
    choices = _admin_choices(data, {ACCOUNT_USERS})
    assert choices["account_user_targets"] == [
        {
            "developer": DEV,
            "user": USER,
            "methods": ["androidpublisher.users.delete"],
            "developer_permissions": [],
            "affected_apps": {APP: []},
            "allow_expiration_change": False,
        },
        {
            "developer": "999",
            "user": "other@example.com",
            "methods": ["androidpublisher.users.patch"],
            "developer_permissions": [],
            "affected_apps": {},
            "allow_expiration_change": True,
        },
    ]


def test_duplicate_target_rows_are_rejected_not_merged(hosted):
    settings, _, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, SIGNING_ENROLL])
    data = fields(SIGNING_ENROLL)
    data.update(
        {key.replace("[0]", "[1]"): value for key, value in copy.copy(data).items() if "[0]" in key}
    )
    response = client.post(connect, data=data, headers={"Origin": settings.issuer})
    assert response.status_code == 400


def test_review_escapes_valid_email_special_characters(hosted):
    settings, _, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, ACCOUNT_USERS])
    data = fields(ACCOUNT_USERS)
    data["account-users[0][user]"] = "user&'quote@example.com"
    response = client.post(connect, data=data, headers={"Origin": settings.issuer})
    assert response.status_code == 200
    assert "user&amp;&#x27;quote@example.com" in response.text
    assert hidden(response.text)["account-users[0][user]"] == "user&'quote@example.com"


def test_get_and_review_keep_original_pending_deadline(hosted):
    settings, app, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, ACCOUNT_DIRECTORY])
    key = parse_qs(urlsplit(connect).query)["request"][0]
    original = app.state.vault.get("consent", key)["consent_expires_at"]
    for _ in range(2):
        assert client.get(connect).status_code == 200
        response = client.post(
            connect, data=fields(ACCOUNT_DIRECTORY), headers={"Origin": settings.issuer}
        )
        assert response.status_code == 200
        pending = app.state.vault.get("consent", key)
        assert pending["consent_expires_at"] == original
        expires = app.state.vault.db.execute(
            "SELECT expires FROM records WHERE namespace='consent' AND key=?",
            (app.state.vault.digest(key),),
        ).fetchone()[0]
        assert expires <= original + 0.01


@pytest.mark.parametrize("admin", [False, True])
def test_legacy_pending_is_not_silently_upgraded_to_review_lifetime(hosted, admin):
    settings, app, client = hosted
    scopes = [READ_SCOPE, ACCOUNT_DIRECTORY] if admin else [READ_SCOPE]
    connect, _, _ = request_consent(client, settings, scopes)
    key = parse_qs(urlsplit(connect).query)["request"][0]
    pending = app.state.vault.get("consent", key)
    pending.pop("consent_expires_at")
    app.state.vault.put("consent", key, pending, 600)
    response = client.get(connect)
    assert response.status_code == (400 if admin else 200)
    assert "consent_expires_at" not in app.state.vault.get("consent", key)


def test_removing_admin_after_review_still_requires_review_of_changed_authority(hosted):
    settings, _, client = hosted
    connect, _, _ = request_consent(client, settings, [READ_SCOPE, ACCOUNT_DIRECTORY])
    response = client.post(
        connect, data=fields(ACCOUNT_DIRECTORY), headers={"Origin": settings.issuer}
    )
    data = hidden(response.text)
    data.pop("directory")
    data.pop("capabilities")
    data["packages"] = APP
    response = client.post(connect, data=data, headers={"Origin": settings.issuer})
    assert response.status_code == 200
    assert "Review exact connection authority" in response.text
    assert "location" not in response.headers
    assert hidden(response.text)["authority_confirmation"] != data["authority_confirmation"]
