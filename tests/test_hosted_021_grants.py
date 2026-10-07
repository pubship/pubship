"""Versioned account/signing authority is exact, bounded and never cross-product."""

import asyncio
import copy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import TokenError
from test_hosted_grants import APP, OTHER, issue
from test_hosted_grants import provider as provider

from pubship.account_access_config import permission_values
from pubship.auth import SCOPES
from pubship.errors import PlayError
from pubship.hosted_grants import (
    ACCOUNT_USERS,
    ADMIN_FIELDS,
    ADMIN_SCOPES,
    READ_SCOPE,
    SIGNING_ENROLL,
    SIGNING_ROTATE,
    V1_SCOPES,
    V2_SCOPES,
    administrative_dimensions,
    consent_authority,
    grant_authority,
)

DEVELOPER, SECOND = "123456", "987654"
USER, OTHER_USER = "Owner@example.test", "other@example.test"
USER_PATCH = "androidpublisher.users.patch"
USER_DELETE = "androidpublisher.users.delete"
GRANT_PATCH = "androidpublisher.grants.patch"
ENROLL = "androidpublisher.appsigning.enrollApp"
ROTATE = "androidpublisher.appsigning.rotateAppSigningKey"
KEY = "projects/example/locations/global/keyRings/release/cryptoKeys/signing/cryptoKeyVersions/1"
KEY2 = KEY[:-1] + "2"
DEV_PERMISSION = sorted(permission_values("User", "developerAccountPermissions"))[0]
APP_PERMISSION = sorted(permission_values("Grant", "appLevelPermissions"))[0]


def empty_dimensions():
    return {
        "account_directory_developers": [],
        "account_user_targets": [],
        "account_grant_targets": [],
        "signing_targets": {},
    }


def dimensions():
    result = empty_dimensions()
    result["account_directory_developers"] = [DEVELOPER]
    result["account_user_targets"] = [
        {
            "developer": DEVELOPER,
            "user": USER,
            "methods": [USER_PATCH, USER_DELETE],
            "developer_permissions": [DEV_PERMISSION],
            "affected_apps": {APP: [APP_PERMISSION]},
            "allow_expiration_change": False,
        },
        {
            "developer": SECOND,
            "user": OTHER_USER,
            "methods": [USER_PATCH],
            "developer_permissions": [],
            "affected_apps": {OTHER: []},
            "allow_expiration_change": True,
        },
    ]
    result["account_grant_targets"] = [
        {
            "developer": DEVELOPER,
            "user": USER,
            "app": APP,
            "methods": [GRANT_PATCH],
            "app_permissions": [APP_PERMISSION],
        },
        {
            "developer": SECOND,
            "user": OTHER_USER,
            "app": OTHER,
            "methods": [GRANT_PATCH],
            "app_permissions": [],
        },
    ]
    result["signing_targets"] = {
        SIGNING_ENROLL: [{"app": APP, "kms_version": KEY}, {"app": OTHER, "kms_version": KEY2}],
        SIGNING_ROTATE: [{"app": APP, "kms_version": KEY2}],
    }
    return result


def connection(provider, fields=None, *, packages=None):
    grant, _, _ = issue(provider)
    grant.update(
        capability_version=3,
        packages=packages or [],
        capability_packages={READ_SCOPE: packages or []},
        appstore_targets={},
        appstore_event_stores=[],
        **(dimensions() if fields is None else fields),
    )
    provider.vault.put("grants", "one", grant, 3600)
    tokens = provider.issue_tokens("one", "client-one", list(grant_authority(grant).scopes))
    principal = asyncio.run(provider.load_access_token(tokens.access_token))
    assert principal is not None
    return grant, tokens, principal


def test_frozen_old_scope_contracts_do_not_grow():
    assert len(V1_SCOPES) == 21 and len(V2_SCOPES) == 28
    assert not ADMIN_SCOPES & V2_SCOPES and V1_SCOPES < V2_SCOPES


@pytest.mark.parametrize("field", sorted(ADMIN_FIELDS))
@pytest.mark.parametrize("version", [0, 1, 2])
def test_old_versions_reject_new_extension_even_empty(provider, field, version):
    grant, _, _ = issue(provider, legacy=version == 0)
    if version == 2:
        grant.update(capability_version=2, appstore_targets={}, appstore_event_stores=[])
    grant[field] = empty_dimensions()[field]
    with pytest.raises(PlayError):
        grant_authority(grant)


@pytest.mark.parametrize("capability", sorted(ADMIN_SCOPES))
def test_v2_cannot_smuggle_admin_scopes_as_ordinary_packages(provider, capability):
    grant, _, _ = issue(provider)
    grant.update(capability_version=2, appstore_targets={}, appstore_event_stores=[])
    grant["capability_packages"][capability] = [APP]
    with pytest.raises(PlayError):
        grant_authority(grant)


@pytest.mark.parametrize("field", sorted(ADMIN_FIELDS))
def test_v3_requires_every_extension(provider, field):
    grant, _, _ = connection(provider)
    del grant[field]
    with pytest.raises(PlayError):
        grant_authority(grant)


def test_exact_targets_and_method_selections_never_union(provider):
    _, _, principal = connection(provider)
    provider.authorize_account_current(principal, USER_PATCH, DEVELOPER, USER)
    provider.authorize_account_current(principal, GRANT_PATCH, DEVELOPER, USER, APP)
    provider.authorize_signing_current(principal, ENROLL, APP, KEY)
    provider.authorize_signing_current(principal, ENROLL, OTHER, KEY2)
    provider.authorize_signing_current(principal, ROTATE, APP, KEY2)
    for method, dev, user, app in [
        (USER_PATCH, DEVELOPER, OTHER_USER, None),
        (USER_PATCH, SECOND, USER, None),
        (USER_DELETE, SECOND, OTHER_USER, None),
        (USER_PATCH, DEVELOPER, USER.lower(), None),
        (USER_PATCH, DEVELOPER, USER, APP),
        (GRANT_PATCH, DEVELOPER, USER, OTHER),
        (GRANT_PATCH, SECOND, OTHER_USER, APP),
        ("androidpublisher.users.list", SECOND, None, None),
        ("androidpublisher.users.list", DEVELOPER, USER, None),
    ]:
        with pytest.raises(PlayError):
            provider.authorize_account_current(principal, method, dev, user, app)
    for method, app, key in [
        (ENROLL, APP, KEY2),
        (ENROLL, OTHER, KEY),
        (ROTATE, APP, KEY),
        (ROTATE, OTHER, KEY2),
    ]:
        with pytest.raises(PlayError):
            provider.authorize_signing_current(principal, method, app, key)
    with pytest.raises(PlayError):
        provider.authorize_current(principal, READ_SCOPE, APP)


def test_deep_immutable_context_preserves_empty_ceilings_and_expiry_control(provider):
    grant, _, principal = connection(provider)
    context = provider.authorize_current(principal)
    row = context.account_user_targets[DEVELOPER, USER]
    assert row.allow_expiration_change is False
    assert context.account_user_targets[SECOND, OTHER_USER].developer_permissions == frozenset()
    assert context.account_user_targets[SECOND, OTHER_USER].affected_apps[OTHER] == frozenset()
    with pytest.raises(TypeError):
        row.affected_apps[APP] = frozenset()
    with pytest.raises(FrozenInstanceError):
        row.allow_expiration_change = True
    with pytest.raises(TypeError):
        context.account_user_targets[DEVELOPER, OTHER_USER] = row
    grant["account_user_targets"][0]["affected_apps"][APP].clear()
    assert row.affected_apps[APP] == frozenset({APP_PERMISSION})
    canonical = administrative_dimensions(context)
    canonical["account_user_targets"][0]["methods"].clear()
    assert row.methods == frozenset({USER_PATCH, USER_DELETE})


@pytest.mark.parametrize("surface", ["directory", "user", "grant", "signing"])
def test_admin_only_empty_packages_refresh_keeps_original_grant(provider, surface):
    fields = empty_dimensions()
    field = {
        "directory": "account_directory_developers",
        "user": "account_user_targets",
        "grant": "account_grant_targets",
        "signing": "signing_targets",
    }[surface]
    fields[field] = dimensions()[field]
    grant, tokens, principal = connection(provider, fields)
    assert not provider.services(principal)[0].packages
    context = provider.authorize_current(principal)
    assert context.authorization_schema_version == 3
    if surface != "directory":
        with pytest.raises(PlayError):
            provider.authorize_account_current(principal, "androidpublisher.users.list", DEVELOPER)
    client = SimpleNamespace(client_id=principal.client_id)
    refresh = asyncio.run(provider.load_refresh_token(client, tokens.refresh_token))
    tokens = asyncio.run(provider.exchange_refresh_token(client, refresh, list(context.scopes)))
    fresh = asyncio.run(provider.load_access_token(tokens.access_token))
    assert administrative_dimensions(
        provider.authorize_current(fresh)
    ) == administrative_dimensions(context)
    assert provider.vault.get("grants", "one") == grant


def test_scope_downselection_hides_dimensions_and_cannot_refresh_back_up(provider):
    grant, tokens, principal = connection(provider)
    tokens = provider.issue_tokens("one", principal.client_id, [READ_SCOPE, ACCOUNT_USERS])
    narrowed = asyncio.run(provider.load_access_token(tokens.access_token))
    context = provider.authorize_current(narrowed)
    assert (
        context.account_user_targets
        and not context.account_directory_developers
        and not context.signing_targets
        and not context.account_grant_targets
    )
    client = SimpleNamespace(client_id=principal.client_id)
    refresh = asyncio.run(provider.load_refresh_token(client, tokens.refresh_token))
    with pytest.raises(TokenError):
        asyncio.run(
            provider.exchange_refresh_token(
                client, refresh, [READ_SCOPE, ACCOUNT_USERS, SIGNING_ENROLL]
            )
        )
    assert provider.vault.get("grants", "one") == grant


BAD_ROWS = [
    ("account_user_targets", "developer", "*"),
    ("account_user_targets", "user", "*"),
    ("account_user_targets", "methods", [GRANT_PATCH]),
    ("account_user_targets", "methods", []),
    ("account_user_targets", "methods", [USER_PATCH, USER_PATCH]),
    ("account_user_targets", "developer_permissions", ["UNKNOWN"]),
    ("account_user_targets", "developer_permissions", [DEV_PERMISSION, DEV_PERMISSION]),
    ("account_user_targets", "affected_apps", {"*": []}),
    ("account_user_targets", "affected_apps", {APP: ["UNKNOWN"]}),
    ("account_user_targets", "affected_apps", {APP: [APP_PERMISSION, APP_PERMISSION]}),
    ("account_user_targets", "allow_expiration_change", 1),
    ("account_user_targets", "extra", True),
    ("account_grant_targets", "methods", [USER_PATCH]),
    ("account_grant_targets", "app", "*"),
    ("account_grant_targets", "app_permissions", ["UNKNOWN"]),
]


@pytest.mark.parametrize("field,key,value", BAD_ROWS)
def test_malformed_target_rows_fail_closed(provider, field, key, value):
    grant, _, _ = connection(provider)
    grant[field][0][key] = value
    with pytest.raises(PlayError, match="invalid, expired or revoked"):
        grant_authority(grant)


@pytest.mark.parametrize("field", ["account_user_targets", "account_grant_targets"])
def test_duplicate_records_never_union_methods_or_permissions(provider, field):
    grant, _, _ = connection(provider)
    grant[field].append(copy.deepcopy(grant[field][0]))
    with pytest.raises(PlayError):
        grant_authority(grant)


@pytest.mark.parametrize(
    "key",
    [
        "projects/example/locations/global/keyRings/release/cryptoKeys/signing",
        KEY.rsplit("/", 1)[0] + "/latest",
        KEY + "/*",
    ],
)
def test_unversioned_or_wildcard_signing_keys_rejected(provider, key):
    grant, _, _ = connection(provider)
    grant["signing_targets"][SIGNING_ENROLL][0]["kms_version"] = key
    with pytest.raises(PlayError):
        grant_authority(grant)


@pytest.mark.parametrize("kind", ["directory", "targets", "apps", "signing"])
def test_global_dimension_bounds(provider, kind):
    grant, _, _ = connection(provider)
    if kind == "directory":
        grant["account_directory_developers"] = [str(v) for v in range(101)]
    elif kind == "targets":
        grant["account_user_targets"] = [
            dict(
                dimensions()["account_user_targets"][0],
                user=f"user{i}@example.test",
                affected_apps={},
            )
            for i in range(100)
        ]
    elif kind == "apps":
        grant["account_user_targets"][0]["affected_apps"] = {
            f"com.example.app{i}": [] for i in range(100)
        }
    else:
        grant["signing_targets"][SIGNING_ENROLL] = [
            {"app": APP, "kms_version": KEY[:-1] + str(i + 1)} for i in range(100)
        ]
    with pytest.raises(PlayError):
        grant_authority(grant)


def test_draft_identity_validators_remain_family_specific(provider):
    fields = empty_dimensions()
    fields["account_grant_targets"] = [dict(dimensions()["account_grant_targets"][0], app="0" * 30)]
    connection(provider, fields)
    fields["signing_targets"] = {SIGNING_ENROLL: [{"app": "0" * 30, "kms_version": KEY}]}
    with pytest.raises(PlayError):
        connection(provider, fields)


def test_explicit_v3_consent_canonicalizes_exact_fields_without_input_aliasing():
    fields = dimensions()
    result = consent_authority(
        [READ_SCOPE, *ADMIN_SCOPES],
        sorted(ADMIN_SCOPES),
        {},
        [],
        ["publisher"],
        {},
        admin_fields=fields,
    )
    assert result["capability_version"] == 3 and result["capability_packages"] == {READ_SCOPE: []}
    fields["account_user_targets"][0]["methods"].clear()
    assert result["account_user_targets"][0]["methods"]


@pytest.mark.parametrize(
    "fault",
    [
        "unrequested",
        "unselected",
        "empty_selected",
        "extra_field",
        "missing_field",
        "google_service",
    ],
)
def test_consent_rejects_malformed_or_unrequested_authority(fault):
    fields, requested, selected, services = (
        dimensions(),
        [READ_SCOPE, *ADMIN_SCOPES],
        sorted(ADMIN_SCOPES),
        ["publisher"],
    )
    if fault == "unrequested":
        requested.remove(ACCOUNT_USERS)
    if fault == "unselected":
        selected.remove(ACCOUNT_USERS)
    if fault == "empty_selected":
        fields["account_user_targets"] = []
    if fault == "extra_field":
        fields["alias"] = []
    if fault == "missing_field":
        del fields["signing_targets"]
    if fault == "google_service":
        services = ["reporting"]
    with pytest.raises(PlayError):
        consent_authority(requested, selected, {}, [], services, {}, admin_fields=fields)


def test_v3_ordinary_only_is_valid_but_wholly_empty_is_not(provider):
    connection(provider, empty_dimensions(), packages=[APP])
    with pytest.raises(PlayError):
        connection(provider, empty_dimensions())


def test_google_completion_keeps_new_consent_schema_and_dimensions(provider):
    from urllib.parse import parse_qs, urlsplit

    selected = consent_authority(
        [READ_SCOPE, *ADMIN_SCOPES],
        sorted(ADMIN_SCOPES),
        {},
        [],
        ["publisher"],
        {},
        admin_fields=dimensions(),
    )
    pending = {
        **selected,
        "client_id": "client-one",
        "packages": [],
        "bucket": "",
        "google_scopes": [SCOPES["publisher"]],
        "params": {
            "scopes": [READ_SCOPE, *ADMIN_SCOPES],
            "code_challenge": "synthetic-challenge",
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "redirect_uri_provided_explicitly": True,
        },
    }
    google = {
        "scope": SCOPES["publisher"],
        "access_token": "synthetic-google-token",
        "refresh_token": "fake-refresh",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    destination = provider.complete_google(pending, google)
    code = parse_qs(urlsplit(destination).query)["code"][0]
    authorization = provider.vault.get("codes", code)
    grant = provider.vault.get("grants", authorization["subject"])
    assert grant["capability_version"] == 3
    assert administrative_dimensions(grant_authority(grant)) == {
        key: selected[key] for key in ADMIN_FIELDS
    }
    pending["capability_version"] = 2
    with pytest.raises(PlayError):
        provider.complete_google(pending, google)
