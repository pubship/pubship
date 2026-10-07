"""Single-attempt transport for explicitly reviewed existing-edit resource writes."""

import json
import urllib.error
import urllib.request
from http.client import HTTPException

from .auth import validate_token
from .edit_writes_contracts import request_spec, validate_response
from .errors import PlayError
from .http import MAX_BYTES, NoRedirect, finite_float
from .provider_transport import bounded_opener, dispatch_check

UNKNOWN_OUTCOME = (
    "Edit resource write outcome unknown; reconcile the caller-owned edit before preparing "
    "another operation. Do not automatically retry. This operation ID has been consumed."
)


def execute(method_id, parameters, body, token, *, deadline=None, _execution_authorization=None):
    spec = request_spec(method_id, parameters, body)
    req = urllib.request.Request(
        spec["url"],
        data=json.dumps(body, allow_nan=False).encode("utf-8") if body is not None else None,
        method=spec["http_method"],
        headers={
            "Authorization": "Bearer " + validate_token(token),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    opener = bounded_opener(NoRedirect(), timeout=15, deadline=deadline)
    dispatch_check(deadline, _execution_authorization)
    try:
        with opener.open(req, timeout=15) as response:
            content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise PlayError(UNKNOWN_OUTCOME)
        data = (
            {}
            if spec["http_method"] == "DELETE" and not content.strip()
            else json.loads(content, parse_float=finite_float, parse_constant=finite_float)
        )
        validate_response(method_id, parameters, body, data)
        return data
    except urllib.error.HTTPError as error:
        error.close()
        raise PlayError(f"Google API HTTP {error.code}. {UNKNOWN_OUTCOME}") from None
    except (
        PlayError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        HTTPException,
        RecursionError,
    ):
        raise PlayError(UNKNOWN_OUTCOME) from None
