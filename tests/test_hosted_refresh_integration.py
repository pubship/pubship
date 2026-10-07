"""Customer refresh uses the bounded adapter and persists only successful responses."""

import asyncio
import json
import time
from unittest.mock import Mock

import pytest
from google.auth.exceptions import TransportError
from test_hosted import connect_account, hosted  # noqa: F401

from pubship.errors import PlayError
from pubship.hosted_token_transport import HostedTokenRequest, _Response
from pubship.oauth import CustomerAuth


@pytest.mark.parametrize("succeeds", [True, False])
def test_customer_refresh_transport_cache_and_failure_preservation(hosted, monkeypatch, succeeds):  # noqa: F811
    settings, app, client = hosted
    _, tokens = connect_account(client, settings, "com.example.alice", "PRIVATE-GOOGLE-ALICE")
    principal = asyncio.run(app.state.oauth.load_access_token(tokens["access_token"]))
    grant = app.state.vault.get("grants", principal.subject)
    grant["google_expires"] = time.time() - 1
    app.state.vault.put("grants", principal.subject, grant, 600)
    saved = app.state.vault.get("grants", principal.subject)
    response = _Response(
        200, json.dumps({"access_token": "synthetic-new", "expires_in": 3600}).encode(), {}
    )
    request = Mock(
        return_value=response, side_effect=None if succeeds else TransportError("PRIVATE-DETAIL")
    )
    monkeypatch.setattr(HostedTokenRequest, "__call__", request)
    adc = Mock(side_effect=AssertionError("local credential fallback"))
    monkeypatch.setattr("google.auth.default", adc)
    auth = CustomerAuth(app.state.oauth, principal.subject)
    if succeeds:
        assert auth.token("publisher") == "synthetic-new"
        assert auth.token("publisher") == "synthetic-new"
        assert app.state.vault.get("grants", principal.subject)["access_token"] == "synthetic-new"
    else:
        with pytest.raises(PlayError) as error:
            auth.token("publisher")
        assert "PRIVATE" not in str(error.value)
        assert app.state.vault.get("grants", principal.subject) == saved
    assert request.call_count == 1
    adc.assert_not_called()
