"""Persisted-authority changes across all hosted mutation families; synthetic I/O."""

import time

import pytest
from test_hosted_edits import transport as transport  # noqa: F401
from test_hosted_grants import APP, OTHER, issue
from test_hosted_grants import provider as provider  # noqa: F401

from pubship.auth import SCOPES
from pubship.errors import PlayError
from pubship.hosted_edits import CAPABILITIES, HostedEditRegistry

FAMILIES = ("listing", "track", "create", "validate", "discard", "commit")


def prepare_family(registry, principal, family):
    parameters = {"packageName": APP}
    if family != "create":
        parameters["editId"] = "edit-1"
    if family == "listing":
        result = registry.prepare(
            principal, family, {**parameters, "language": "en-US"}, {"title": "new"}
        )
    elif family == "track":
        result = registry.prepare(
            principal,
            family,
            {**parameters, "track": "production"},
            [{"status": "draft", "versionCodes": ["1"]}],
            acknowledge_effects=True,
        )
    else:
        result = registry.prepare(
            principal, "lifecycle", parameters, action=family, acknowledge_effects=True
        )
    return family if family in {"listing", "track"} else "lifecycle", result["operation_id"]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    "change",
    [
        "package-removed",
        "service-removed",
        "grant-expired",
        "access-deleted",
        "access-expired",
        "disconnect",
    ],
)
def test_persisted_authority_change_blocks_apply_without_harming_other_connection(
    provider, transport, family, change
):
    capability = CAPABILITIES[family]
    grant, _, owner = issue(provider, capabilities={capability: [APP]})
    _, _, other = issue(
        provider,
        subject="other-connection",
        client=owner.client_id,
        capabilities={capability: [APP]},
    )
    registry = HostedEditRegistry(provider)
    try:
        kind, operation = prepare_family(registry, owner, family)
        other_kind, other_operation = prepare_family(registry, other, family)
        if change == "disconnect":
            provider.disconnect(owner.subject)
            assert operation not in registry._records
        elif change == "access-deleted":
            provider.vault.delete("access", owner.token)
        elif change == "access-expired":
            access = provider.vault.get("access", owner.token)
            access["expires_at"] = int(time.time()) - 1
            # A stale in-memory principal must not override the persisted expiry.
            provider.vault.put("access", owner.token, access, 3600)
        else:
            if change == "package-removed":
                grant["capability_packages"][capability] = [OTHER]
            elif change == "service-removed":
                grant["google_scopes"] = [SCOPES["reporting"]]
            else:
                grant["expires"] = time.time() - 1
            provider.vault.put("grants", owner.subject, grant, 3600)

        transport.reads.reset_mock()
        with pytest.raises(PlayError):
            registry.apply(owner, kind, operation, operation)
        transport.reads.assert_not_called()
        for mutation in (transport.patch, transport.put, transport.lifecycle):
            mutation.assert_not_called()

        # Identical Google credentials, package and MCP client do not bind grants
        # together: a separate connection retains its preparation and authority.
        result = registry.apply(other, other_kind, other_operation, other_operation)
        assert result["executed"]
        assert sum(m.call_count for m in (transport.patch, transport.put, transport.lifecycle)) == 1
    finally:
        registry.close()


@pytest.mark.parametrize("family", ["listing", "track", "validate", "discard", "commit"])
def test_oversized_provider_snapshot_releases_global_reservation(provider, transport, family):
    _, _, owner = issue(provider, capabilities={CAPABILITIES[family]: [APP]})
    registry = HostedEditRegistry(provider, max_total=1)
    try:
        oversized = transport.get("synthetic-edit", "synthetic-token")
        oversized["provider_extension"] = "x" * (64 * 1024)
        transport.reads.return_value = oversized
        transport.reads.side_effect = None
        with pytest.raises(PlayError, match="snapshot exceeds"):
            prepare_family(registry, owner, family)
        assert not registry._records
        for mutation in (transport.patch, transport.put, transport.lifecycle):
            mutation.assert_not_called()

        transport.reads.side_effect = transport.get
        kind, operation = prepare_family(registry, owner, family)
        assert registry.apply(owner, kind, operation, operation)["executed"]
        assert not registry._records
    finally:
        registry.close()
