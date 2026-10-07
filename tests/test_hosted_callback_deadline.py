"""Total callback token-exchange deadline; all OAuth responses are synthetic."""

import time
from unittest.mock import AsyncMock, Mock

import anyio
import pytest
from test_hosted import begin
from test_hosted import hosted as hosted  # noqa: F401

from pubship import hosted as module


@pytest.mark.parametrize("phase", ["fetch", "exit"])
def test_callback_total_deadline_consumes_state_without_grant(hosted, monkeypatch, phase):
    settings, app, client = hosted
    pending = begin(client, settings)
    completed = Mock(side_effect=AssertionError("Expired exchange must not create a grant"))
    monkeypatch.setattr(app.state.oauth, "complete_google", completed)
    monkeypatch.setattr(module, "GOOGLE_EXCHANGE_TIMEOUT", 0.03)

    async def delayed(*args, **kwargs):
        # Continuous small progress does not reset the total deadline.
        for _ in range(100):
            await anyio.sleep(0.01)
        return {}

    monkeypatch.setattr(
        module.AsyncOAuth2Client,
        "fetch_token",
        delayed if phase == "fetch" else AsyncMock(return_value={}),
    )
    if phase == "exit":
        monkeypatch.setattr(module.AsyncOAuth2Client, "__aexit__", delayed)
    started = time.monotonic()
    response = client.get(
        "/google/callback", params={"state": pending["state"], "code": "synthetic"}
    )
    assert response.status_code == 400
    assert "Google connection failed" in response.text
    assert time.monotonic() - started < 0.5
    completed.assert_not_called()
    assert app.state.vault.get("google_state", pending["state"]) is None
