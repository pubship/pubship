"""Fixed-route, single-attempt Publisher edit lifecycle transport."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

from .auth import validate_token
from .config import PACKAGE
from .contracts import validate_request
from .errors import PlayError
from .http import MAX_BYTES, NoRedirect, finite_float
from .play import path_segment
from .provider_transport import bounded_opener, dispatch_check

METHODS = {
    "create": "androidpublisher.edits.insert",
    "validate": "androidpublisher.edits.validate",
    "discard": "androidpublisher.edits.delete",
    "commit": "androidpublisher.edits.commit",
}
UNKNOWN_OUTCOME = (
    "Publisher edit operation outcome unknown; reconcile the app and edit before preparing "
    "another operation. Do not automatically retry. This operation ID has been consumed."
)


def normalize_parameters(action, parameters):
    """Reject everything outside the intentionally narrower lifecycle contract."""
    if not isinstance(action, str) or action not in METHODS:
        raise PlayError("Action must be create, validate, discard or commit.")
    required = {"packageName"} if action == "create" else {"packageName", "editId"}
    optional = (
        {"changesNotSentForReview", "changesInReviewBehavior"} if action == "commit" else set()
    )
    if (
        not isinstance(parameters, dict)
        or not required <= parameters.keys()
        or not parameters.keys() <= required | optional
    ):
        raise PlayError("Unexpected or missing edit lifecycle parameters.")
    path_segment(parameters["packageName"], limit=255)
    if not PACKAGE.fullmatch(parameters["packageName"]):
        raise PlayError("Invalid Android package name.")
    if action != "create":
        path_segment(parameters["editId"], limit=1024)
    result = dict(parameters)
    if action == "commit":
        flag = parameters.get("changesNotSentForReview", False)
        if type(flag) is not bool:
            raise PlayError("changesNotSentForReview must be a boolean.")
        if parameters.get("changesInReviewBehavior", "ERROR_IF_IN_REVIEW") != "ERROR_IF_IN_REVIEW":
            raise PlayError("changesInReviewBehavior must be ERROR_IF_IN_REVIEW.")
        result.update(changesNotSentForReview=flag, changesInReviewBehavior="ERROR_IF_IN_REVIEW")
    validate_request(METHODS[action], result, {} if action == "create" else None)
    return result


def edit_url(parameters):
    return (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        + parameters["packageName"]
        + "/edits/"
        + parameters["editId"]
    )


def request_spec(action, parameters):
    parameters = normalize_parameters(action, parameters)
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        + parameters["packageName"]
        + "/edits"
        if action == "create"
        else edit_url(parameters)
    )
    if action in {"validate", "commit"}:
        url += ":" + action
    if action == "commit":
        url += "?" + urllib.parse.urlencode(
            {
                "changesNotSentForReview": str(parameters["changesNotSentForReview"]).lower(),
                "changesInReviewBehavior": "ERROR_IF_IN_REVIEW",
            }
        )
    return {
        "method": METHODS[action],
        "http_method": "DELETE" if action == "discard" else "POST",
        "url": url,
        "parameters": parameters,
        "body": {} if action == "create" else None,
    }


def validate_response(action, data, parameters):
    if action == "discard":
        if not isinstance(data, dict) or data:
            raise PlayError("Publisher returned an unexpected delete response.")
        return
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("id"), str)
        or not isinstance(data.get("expiryTimeSeconds"), str)
        or not re.fullmatch(r"[0-9]{1,20}", data["expiryTimeSeconds"])
        or (action != "create" and data["id"] != parameters["editId"])
    ):
        raise PlayError("Publisher returned unexpected edit identity or expiry metadata.")
    path_segment(data["id"], limit=1024)


def execute(action, parameters, token, *, deadline=None, _execution_authorization=None):
    """Validate before dispatch, retain safe HTTP codes, and never retry."""
    spec = request_spec(action, parameters)
    req = urllib.request.Request(
        spec["url"],
        data=b"{}" if action == "create" else None,
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
            if action == "discard" and not content
            else json.loads(content, parse_float=finite_float, parse_constant=finite_float)
        )
        validate_response(action, data, spec["parameters"])
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
