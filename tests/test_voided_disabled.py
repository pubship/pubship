"""No local or HTTP method grant may deliver voided-purchase data to an AI client."""

from unittest.mock import Mock, patch

import pytest
from test_hosted import call_tool, connect_account
from test_hosted import hosted as hosted  # noqa: F401

from pubship.catalog import IMPLEMENTED, list_methods
from pubship.config import Settings
from pubship.errors import PlayError
from pubship.hosted_catalog import HOSTED_CAPABILITIES
from pubship.publisher_reads import PublisherReads

METHOD = "androidpublisher.purchases.voidedpurchases.list"


def test_inventory_keeps_definition_but_no_execution_mapping():
    assert METHOD not in IMPLEMENTED and METHOD not in HOSTED_CAPABILITIES
    row = list_methods(status="policy_disabled")["methods"]
    assert len(row) == 1 and row[0]["id"] == METHOD and row[0]["tool"] is None


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("sensitive", [True, False])
def test_all_read_entrypoints_reject_before_provider_credentials(local, sensitive):
    auth = Mock()
    settings = Settings(
        packages=frozenset({"com.example.app"}),
        sensitive_read_packages=frozenset({"com.example.app"}),
    )
    read = PublisherReads(
        settings, auth, local=local, _execution_authorization=Mock() if not local else None
    )
    with patch("pubship.http.get") as get, pytest.raises(PlayError):
        (read.read_sensitive if sensitive else read.read)(
            METHOD, {"packageName": "com.example.app"}
        )
    auth.token.assert_not_called()
    get.assert_not_called()


def test_authenticated_http_cannot_dispatch_voided_method(hosted):
    settings, _, client = hosted
    _, tokens = connect_account(client, settings, "com.example.app", "synthetic-access")
    with patch("pubship.http.get") as get:
        response = call_tool(
            client,
            tokens["access_token"],
            "read_sensitive_publisher",
            {"method": METHOD, "parameters": {"packageName": "com.example.app"}},
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"]
    get.assert_not_called()
