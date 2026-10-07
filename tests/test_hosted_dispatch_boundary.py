"""Hosted intent must remain valid after the transport constructs its request."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_hosted_edits import PACKAGE, Provider, principal

from pubship import edit_lifecycle_http, hosted_edits
from pubship.errors import PlayError
from pubship.hosted_edits import HostedEditRegistry


@pytest.mark.parametrize("cause", ["expiry", "shutdown"])
def test_legacy_lifecycle_checks_authority_at_actual_dispatch(monkeypatch, cause):
    registry, who = HostedEditRegistry(Provider()), principal()
    try:
        prepared = registry.prepare(
            who,
            "lifecycle",
            {"packageName": PACKAGE},
            acknowledge_effects=True,
            action="create",
        )
        operation = prepared["operation_id"]
        record = registry._records[operation]
        clock = [time.monotonic()]
        monkeypatch.setattr(
            hosted_edits, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
        )
        original = edit_lifecycle_http.request_spec

        def construction(*args, **kwargs):
            spec = original(*args, **kwargs)
            if cause == "expiry":
                clock[0] = record.deadline + 1
            else:
                registry.close()
            return spec

        monkeypatch.setattr(edit_lifecycle_http, "request_spec", construction)
        opened = Mock(side_effect=AssertionError("No provider mutation is allowed"))
        monkeypatch.setattr("urllib.request.OpenerDirector.open", opened)
        with pytest.raises(PlayError, match="expired|shutting down"):
            registry.apply(who, "lifecycle", operation, operation)
        opened.assert_not_called()
        assert not registry._records
    finally:
        registry.close()
