"""Bounded, single-attempt transport for hosted Google OAuth token refresh.

Local ADC keeps its own transport. This adapter accepts only the hosted token
endpoint, retains TLS verification, and disables redirects and environment
proxies. System DNS resolution has the shared transport's documented limitation.
"""

import json
import urllib.error
import urllib.request
from http.client import HTTPException

from google.auth.exceptions import TransportError
from google.auth.transport import Request, Response

from .errors import PlayError
from .http import NoRedirect, finite_float
from .provider_transport import bounded_opener

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 64 * 1024
ERROR = "Hosted Google token request failed or returned unavailable data."


class _Response(Response):
    def __init__(self, status, data, headers):
        self._status, self._data, self._headers = status, data, headers

    @property
    def status(self):
        return self._status

    @property
    def data(self):
        return self._data

    @property
    def headers(self):
        return self._headers


class HostedTokenRequest(Request):
    """Google Auth request interface, with a fixed hosted-only network policy."""

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        # Caller timeout/TLS/proxy options must never widen the hosted policy.
        if url != TOKEN_ENDPOINT or method != "POST" or kwargs:
            raise TransportError(ERROR)
        try:
            request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
            opener = bounded_opener(
                NoRedirect(),
                timeout=TIMEOUT_SECONDS,
                proxy_handler=urllib.request.ProxyHandler({}),
            )
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if response.status != 200 or len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError()
                decoded = json.loads(data, parse_float=finite_float, parse_constant=finite_float)
                if not isinstance(decoded, dict):
                    raise ValueError()
                return _Response(response.status, data, dict(response.headers))
        except urllib.error.HTTPError as error:
            error.close()
            # Returning 429/5xx lets google-auth retry outside our one-attempt
            # budget. Raise instead, without retaining provider error bodies.
            raise TransportError(ERROR) from None
        except (PlayError, OSError, ValueError, TypeError, HTTPException, RecursionError):
            raise TransportError(ERROR) from None
