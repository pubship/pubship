"""Bounded GET and explicitly scoped Reporting query transport. No mutations."""

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

from .auth import validate_token
from .errors import PlayError
from .provider_transport import bounded_opener

MAX_BYTES = 20 * 1024 * 1024
# Large bulk reports need a bounded transfer budget distinct from socket idle time.
READ_TOTAL_SECONDS = 120
# An actual 204 has no representation; it is not equivalent to JSON null or {}.
NO_CONTENT = object()
HOSTS = {
    "androidpublisher.googleapis.com",
    "playdeveloperreporting.googleapis.com",
    "storage.googleapis.com",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PlayError("Unexpected redirect blocked; credentials were not forwarded.")


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Response contains a non-finite number.")
    return number


def get(url, token, *, raw=False, allow_no_content=False):
    return _read(url, token, raw=raw, allow_no_content=allow_no_content)


def reporting_query(url, token, body):
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "playdeveloperreporting.googleapis.com"
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(
            r"/v1beta1/apps/[A-Za-z][A-Za-z0-9_.]*/[A-Za-z]+MetricSet:query", parsed.path
        )
    ):
        raise PlayError("POST is restricted to read-only Developer Reporting metric queries.")
    try:
        content = json.dumps(body, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise PlayError("Query must contain finite JSON data.") from None
    if len(content) > 64 * 1024:
        raise PlayError("Query exceeds 64 KiB.")
    return _read(url, token, content=content)


def _read(url, token, *, raw=False, content=None, allow_no_content=False):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in HOSTS:
        raise PlayError("Request is outside the allowed Google API hosts.")
    req = urllib.request.Request(
        url,
        data=content,
        headers={
            "Authorization": "Bearer " + validate_token(token),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with bounded_opener(NoRedirect(), timeout=15, total_timeout=READ_TOTAL_SECONDS).open(
            req, timeout=15
        ) as response:
            status = getattr(response, "status", None)
            content = response.read(MAX_BYTES + 1)
        if allow_no_content and not raw and status == 204:
            if content:
                raise ValueError("A no-content response unexpectedly contained data.")
            return NO_CONTENT
        if len(content) > MAX_BYTES:
            raise PlayError("Response exceeds 20 MiB; narrow the report.")
        return (
            content
            if raw
            else json.loads(content, parse_float=finite_float, parse_constant=finite_float)
        )
    except urllib.error.HTTPError as error:
        error.close()
        hints = {
            401: "Renew authentication.",
            403: "Check API enablement, Play permissions and app access.",
            404: "Resource unavailable; a missing report is not zero activity.",
            429: "Provider quota exceeded; retry later.",
        }
        raise PlayError(
            f"Google API HTTP {error.code}. {hints.get(error.code, 'Provider request failed; retry later.')} No writes were attempted."
        ) from None
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        HTTPException,
        RecursionError,
    ):
        raise PlayError(
            "Google API request failed or returned invalid data; retry later."
        ) from None
