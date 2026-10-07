"""A progressing bulk report may outlast idle time, but never its total budget."""

import urllib.request

import pytest
from test_provider_transport_deadlines import upstream

from pubship import http
from pubship.errors import PlayError
from pubship.provider_transport import bounded_opener


@pytest.mark.parametrize("size,succeeds", [(8, True), (50, False)])
def test_bulk_report_progress_uses_separate_total_budget(monkeypatch, size, succeeds):
    body = b"a" * size
    with upstream(
        f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\n\r\n".encode(), body, interval=0.2
    ) as (url, requests):
        # Scale production budgets only, retaining their actual relationship.
        # One-second idle headroom accommodates loaded CI scheduling; the short
        # response still outlasts idle time, and the long one exceeds total time.
        # Loopback wire tests must not discover machine-specific proxy settings.
        # Redirect the synthetic request locally after normal host validation.
        def opener(handler, *, timeout, total_timeout=None):
            actual = bounded_opener(
                handler,
                timeout=timeout / 15,
                total_timeout=None if total_timeout is None else total_timeout / 15,
                proxy_handler=urllib.request.ProxyHandler({}),
            )

            class Local:
                def open(self, request, *, timeout):
                    return actual.open(
                        urllib.request.Request(
                            url, data=request.data, headers=dict(request.headers)
                        ),
                        timeout=timeout / 15,
                    )

            return Local()

        monkeypatch.setattr(http, "bounded_opener", opener)
        if succeeds:
            assert (
                http.get("https://storage.googleapis.com/synthetic", "synthetic-token", raw=True)
                == body
            )
        else:
            with pytest.raises(PlayError):
                http.get("https://storage.googleapis.com/synthetic", "synthetic-token", raw=True)
        assert len(requests) == 1
