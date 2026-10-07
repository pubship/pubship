"""Hosted discovery distinguishes exact store authority and byte transport readiness."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_hosted_020_grants import APP, CATALOG, SECOND_STORE, STORE, store_connection
from test_hosted_grants import provider as provider

from pubship.coverage import (
    ACCOUNT_READ_METHODS,
    ACCOUNT_WRITE_METHODS,
    APPSTORE_READ_METHODS,
    APPSTORE_WRITE_METHODS,
    ARTIFACT_METHODS,
    SHARING_METHODS,
    SIGNING_METHODS,
)
from pubship.hosted_catalog import (
    HOSTED_APPSTORE_CAPABILITIES,
    HOSTED_ARTIFACT_CAPABILITIES,
    HOSTED_CAPABILITIES,
    HOSTED_SHARING_CAPABILITIES,
    execution_info,
)
from pubship.hosted_grants import READ_SCOPE, grant_authority
from pubship.hosted_tools import build_hosted_tools


def test_distribution_inventory_remains_exact_eighteen_alongside_final_nine():
    groups = [
        set(HOSTED_ARTIFACT_CAPABILITIES),
        set(HOSTED_SHARING_CAPABILITIES),
        set(HOSTED_APPSTORE_CAPABILITIES),
    ]
    assert sum(map(len, groups)) == len(set.union(*groups)) == 18
    assert (
        set.union(*groups)
        == ARTIFACT_METHODS | SHARING_METHODS | APPSTORE_READ_METHODS | APPSTORE_WRITE_METHODS
    )
    assert len(HOSTED_CAPABILITIES) == 170
    assert (
        ACCOUNT_READ_METHODS | ACCOUNT_WRITE_METHODS | SIGNING_METHODS
    ) <= HOSTED_CAPABILITIES.keys()
    assert len(ACCOUNT_READ_METHODS | ACCOUNT_WRITE_METHODS | SIGNING_METHODS) == 9


def test_store_discovery_never_flattens_pairs_into_packages(provider):
    _, _, principal = store_connection(provider, events=True)
    authorization = provider.authorize_current(principal)
    view = execution_info("androidpublisher.appstorecatalog.recentappviews.get", authorization)
    assert view["available_for_connection"]
    assert view["authorized_packages"] == []
    assert view["authorized_appstore_targets"] == {STORE: [APP], SECOND_STORE: ["com.example.two"]}
    assert view["authorized_appstore_event_stores"] == []
    events = execution_info(
        "androidpublisher.appstorecatalog.recentupdateevents.list", authorization
    )
    assert events["available_for_connection"]
    assert events["authorized_appstore_targets"] == {}
    assert events["authorized_appstore_event_stores"] == [STORE]
    assert not execution_info("androidpublisher.reviews.list", authorization)[
        "available_for_connection"
    ]


def test_connection_discovery_retains_base_empty_and_separate_store_dimensions(provider):
    _, _, principal = store_connection(provider, events=True)
    registry = Mock()
    registry._subject.side_effect = lambda p: p.subject
    tool = next(
        t
        for t in build_hosted_tools(provider, registry, lambda: principal)
        if t.name == "get_connection_capabilities"
    )
    result = tool.fn()
    assert result["capability_packages"] == {READ_SCOPE: []}
    assert result["authorized_appstore_targets"][CATALOG][STORE] == [APP]
    assert result["authorized_appstore_event_stores"] == [STORE]
    assert result["authorization_schema_version"] == 2
    assert result["binary_transfers_enabled"] is False


@pytest.mark.parametrize("enabled", [False, True])
def test_media_availability_requires_runtime_transfer_store(provider, enabled):
    grant, _, principal = store_connection(provider)
    grant["appstore_targets"]["play:appstore-media"] = {STORE: [APP]}
    provider.vault.put("grants", "one", grant, 600)
    token = provider.issue_tokens("one", "client-one", list(grant_authority(grant).scopes))
    principal = asyncio.run(provider.load_access_token(token.access_token))
    if enabled:
        provider.transfers = SimpleNamespace(enabled=True)
    info = execution_info(
        "androidpublisher.appstoreappsreview.uploadapk", provider.authorize_current(principal)
    )
    assert info["implemented"] is True
    assert info["available_for_connection"] is enabled
    assert info["authorized_packages"] == []
    assert info["authorized_appstore_targets"] == ({STORE: [APP]} if enabled else {})


def test_binary_tool_contracts_accept_handles_not_paths_or_tokens(provider):
    _, _, principal = store_connection(provider)
    tools = {tool.name: tool for tool in build_hosted_tools(provider, Mock(), lambda: principal)}
    for name in (
        "prepare_artifact_upload",
        "prepare_internal_sharing_upload",
        "prepare_appstore_operation",
    ):
        properties = tools[name].parameters["properties"]
        assert "upload_handle" in properties
        assert not {"filename", "file_path", "token", "access_token", "url"} & properties.keys()
        assert tools[name].parameters["additionalProperties"] is False
    assert "body" in tools["begin_upload_transfer"].parameters["properties"]
    assert not tools["apply_appstore_operation"].annotations.idempotent_hint
    assert not tools["apply_internal_sharing_upload"].annotations.read_only_hint
