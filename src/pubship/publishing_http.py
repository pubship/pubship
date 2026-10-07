"""Single-attempt transport restricted to existing localized listing text PATCHes."""

import json
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

from .auth import validate_token
from .errors import PlayError
from .http import MAX_BYTES, NoRedirect, finite_float
from .listing_preview import validate_preview
from .provider_transport import bounded_opener, dispatch_check

UNKNOWN_OUTCOME = (
    "Listing PATCH outcome unknown; reconcile the caller-owned edit before preparing another "
    "operation. Do not automatically retry. This operation ID has been consumed."
)


def listing_patch(url, token, body, *, deadline=None, _execution_authorization=None):
    """Validate the fixed route and body before dispatch; never retry a mutation."""
    parsed = urllib.parse.urlsplit(url)
    parts = parsed.path.split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "androidpublisher.googleapis.com"
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
        or url != "https://androidpublisher.googleapis.com" + parsed.path
        or len(parts) != 9
        or parts[:4] != ["", "androidpublisher", "v3", "applications"]
        or parts[5] != "edits"
        or parts[7] != "listings"
    ):
        raise PlayError("PATCH is restricted to an existing Publisher localized listing.")
    validate_preview({"packageName": parts[4], "editId": parts[6], "language": parts[8]}, body)
    try:
        content = json.dumps(body, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise PlayError("Listing changes must contain finite JSON data.") from None
    if len(content) > 64 * 1024:
        raise PlayError("Listing changes exceed 64 KiB.")
    req = urllib.request.Request(
        url,
        data=content,
        method="PATCH",
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
        return json.loads(content, parse_float=finite_float, parse_constant=finite_float)
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
        # Includes redirects: the original PATCH was dispatched even though its
        # redirect was blocked and credentials were never forwarded.
        raise PlayError(UNKNOWN_OUTCOME) from None
