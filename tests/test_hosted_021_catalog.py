"""Account/signing discovery exposes exact dimensions, never ordinary app grants."""

import asyncio

import pytest
from test_hosted_021_grants import (
    APP,
    DEVELOPER,
    ENROLL,
    KEY,
    KEY2,
    OTHER,
    ROTATE,
    USER,
    USER_DELETE,
    USER_PATCH,
    connection,
)
from test_hosted_grants import issue
from test_hosted_grants import provider as provider

from pubship.coverage import ACCOUNT_READ_METHODS, ACCOUNT_WRITE_METHODS, SIGNING_METHODS
from pubship.hosted_catalog import (
    HOSTED_ACCOUNT_CAPABILITIES,
    HOSTED_CAPABILITIES,
    HOSTED_SIGNING_CAPABILITIES,
    execution_info,
)
from pubship.hosted_grants import ACCOUNT_USERS, READ_SCOPE


def test_final_nine_method_delta_is_exact():
    assert set(HOSTED_ACCOUNT_CAPABILITIES) == ACCOUNT_READ_METHODS | ACCOUNT_WRITE_METHODS
    assert set(HOSTED_SIGNING_CAPABILITIES) == SIGNING_METHODS
    assert len(HOSTED_ACCOUNT_CAPABILITIES) + len(HOSTED_SIGNING_CAPABILITIES) == 9
    assert len(HOSTED_CAPABILITIES) == 170


@pytest.mark.parametrize(
    "method", sorted(ACCOUNT_READ_METHODS | ACCOUNT_WRITE_METHODS | SIGNING_METHODS)
)
def test_discovery_respects_selected_methods_and_does_not_label_ids_as_packages(provider, method):
    _, _, principal = connection(provider)
    info = execution_info(method, provider.authorize_current(principal))
    assert info["implemented"] and info["authorization_schema_version"] == 3
    assert info["authorized_packages"] == []
    assert info["available_for_connection"] == (
        method
        not in {
            "androidpublisher.users.create",
            "androidpublisher.grants.create",
            "androidpublisher.grants.delete",
        }
    )


def test_user_discovery_is_filtered_per_method_and_keeps_permission_ceilings(provider):
    _, _, principal = connection(provider)
    context = provider.authorize_current(principal)
    patch = execution_info(USER_PATCH, context)["authorized_account_user_targets"]
    delete = execution_info(USER_DELETE, context)["authorized_account_user_targets"]
    assert len(patch) == 2 and len(delete) == 1
    assert delete[0]["developer"] == DEVELOPER and delete[0]["user"] == USER
    assert delete[0]["allow_expiration_change"] is False
    assert delete[0]["affected_apps"] and delete[0]["developer_permissions"]


def test_signing_discovery_does_not_cross_product_apps_and_keys(provider):
    _, _, principal = connection(provider)
    context = provider.authorize_current(principal)
    assert execution_info(ENROLL, context)["authorized_signing_targets"] == [
        {"app": APP, "kms_version": KEY},
        {"app": OTHER, "kms_version": KEY2},
    ]
    assert execution_info(ROTATE, context)["authorized_signing_targets"] == [
        {"app": APP, "kms_version": KEY2}
    ]


def test_old_connection_has_no_new_dimensions(provider):
    _, _, principal = issue(provider)
    context = provider.authorize_current(principal)
    assert (
        not context.account_directory_developers
        and not context.account_user_targets
        and not context.account_grant_targets
        and not context.signing_targets
    )
    for method in ACCOUNT_READ_METHODS | ACCOUNT_WRITE_METHODS | SIGNING_METHODS:
        info = execution_info(method, context)
        assert info["implemented"] and not info["available_for_connection"]


def test_narrowed_token_discovery_cannot_reveal_other_authority(provider):
    _, _, principal = connection(provider)
    token = provider.issue_tokens("one", principal.client_id, [READ_SCOPE, ACCOUNT_USERS])
    narrow = asyncio.run(provider.load_access_token(token.access_token))
    context = provider.authorize_current(narrow)
    assert execution_info(USER_PATCH, context)["available_for_connection"]
    assert execution_info(ENROLL, context)["authorized_signing_targets"] == []
    assert (
        execution_info("androidpublisher.users.list", context)[
            "authorized_account_directory_developers"
        ]
        == []
    )
