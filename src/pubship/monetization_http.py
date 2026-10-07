"""Single-attempt bounded commerce transport with typed, qualified GET observations."""

import json
import urllib.error
import urllib.request
from http.client import HTTPException
from urllib.parse import urlsplit

from .auth import validate_token
from .errors import PlayError
from .http import MAX_BYTES, NO_CONTENT, NoRedirect, finite_float
from .monetization_contracts import request_spec
from .provider_transport import bounded_opener, dispatch_check

UNKNOWN_OUTCOME = (
    "Live commerce mutation outcome unknown; reconcile all targets before preparing another "
    "operation. Do not automatically retry. The operation ID has been consumed."
)


def _send(
    url,
    token,
    *,
    method="GET",
    body=None,
    mutation=False,
    empty_success=False,
    unavailable_is_observation=False,
    deadline=None,
    _execution_authorization=None,
):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "androidpublisher.googleapis.com"
        or parsed.fragment
    ):
        raise PlayError("Commerce requests require the fixed Google Publisher host.")
    request = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body, allow_nan=False).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + validate_token(token),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    opener = bounded_opener(NoRedirect(), timeout=15, deadline=deadline)
    dispatch_check(deadline, _execution_authorization)
    try:
        with opener.open(request, timeout=15) as response:
            status = response.status
            content = response.read(MAX_BYTES + 1)
        if status not in {200, 201, 204} or len(content) > MAX_BYTES:
            raise ValueError("Unexpected commerce response")
        if status == 204:
            if content:
                raise ValueError("Unexpected no-content representation")
            return status, NO_CONTENT
        if (method == "DELETE" or empty_success) and not content.strip():
            return status, {}
        data = json.loads(content, parse_float=finite_float, parse_constant=finite_float)
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return status, data
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if not mutation and (method == "GET" or unavailable_is_observation) and status == 404:
            return 404, None
        raise PlayError(
            f"Google API HTTP {status}. "
            + (UNKNOWN_OUTCOME if mutation else "Commerce read failed; no writes attempted.")
        ) from None
    except (
        PlayError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        HTTPException,
        RecursionError,
    ):
        raise PlayError(
            UNKNOWN_OUTCOME
            if mutation
            else "Commerce read failed or returned invalid data; no writes attempted."
        ) from None


def observe(url, token):
    status, data = _send(url, token)
    return {"http_status": status, "data": None if data is NO_CONTENT else data}


def query(method, parameters, body, token):
    spec = request_spec(method, parameters, body, query=True)
    status, data = _send(spec["url"], token, method="POST", body=body)
    return {
        "http_status": status,
        "content_present": data is not NO_CONTENT,
        "data": None if data is NO_CONTENT else data,
    }


def execute(method, parameters, body, token, *, deadline=None, _execution_authorization=None):
    spec = request_spec(method, parameters, body)
    status, data = _send(
        spec["url"],
        token,
        method=spec["http_method"],
        body=body,
        mutation=True,
        empty_success=spec.get("empty_response", False),
        deadline=deadline,
        _execution_authorization=_execution_authorization,
    )
    if spec["http_method"] == "DELETE" or spec.get("empty_response", False):
        if data is NO_CONTENT or data == {}:
            return {}
        raise PlayError(UNKNOWN_OUTCOME)
    if data is NO_CONTENT:
        raise PlayError(UNKNOWN_OUTCOME)
    return data


def observe_query(method, parameters, body, token):
    spec = request_spec(method, parameters, body, query=True)
    status, data = _send(
        spec["url"], token, method="POST", body=body, unavailable_is_observation=True
    )
    return {"http_status": status, "data": None if data is NO_CONTENT else data}
