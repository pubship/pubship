"""Fixed, bounded, redacted account routes; no retries or redirects."""

import json
import time
import urllib.error
import urllib.request
from functools import lru_cache
from http.client import HTTPException

from jsonschema import Draft202012Validator

from .account_access_config import READ_METHOD
from .account_access_contracts import request_spec, target
from .auth import validate_token
from .contracts import definitions, json_schema
from .errors import PlayError
from .http import NoRedirect, finite_float
from .provider_transport import bounded_opener, dispatch_check

MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 100
MAX_USERS = 10000
UNKNOWN_OUTCOME = (
    "Account mutation outcome unknown; reconcile permissions in Play Console before preparing "
    "again. Do not automatically retry. The operation ID is consumed."
)


def request(spec, token, *, deadline=None, _execution_authorization=None, _max_bytes=None):
    limit = MAX_BYTES if _max_bytes is None else _max_bytes
    if type(limit) is not int or not 0 < limit <= MAX_BYTES:
        raise PlayError("Invalid account response byte limit.")
    # Reconstruct the URL to prevent caller-supplied endpoints or paths.
    spec = request_spec(spec["method"], spec["parameters"], spec["body"])
    mutation = spec["method"] != READ_METHOD
    body = spec["body"]
    req = urllib.request.Request(
        spec["url"],
        method=spec["http_method"],
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
        with opener.open(req, timeout=15) as response:
            status = response.status
            content = response.read(limit + 1)
        if status not in {200, 201, 204} or len(content) > limit:
            raise ValueError
        # A valid write response remains success even if its grant expires in flight.
        if not mutation:
            if _execution_authorization is not None:
                _execution_authorization()
            if deadline is not None:
                fresh(deadline)
        if spec["operation"] == "delete" and not content.strip():
            return {}
        if status == 204:
            raise ValueError
        result = json.loads(content, parse_float=finite_float, parse_constant=finite_float)
        if not isinstance(result, dict):
            raise ValueError
        if spec["operation"] == "delete" and result:
            raise ValueError
        validate_response(spec, result)
        return result
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise PlayError(
            f"Google API HTTP {status}. "
            + (UNKNOWN_OUTCOME if mutation else "Account observation unavailable.")
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
            else "Account observation returned invalid or unavailable data."
        ) from None


@lru_cache(maxsize=3)
def _validator(resource):
    schemas = definitions()["androidpublisher"]["schemas"]
    schema = {
        "$ref": "#/$defs/" + resource,
        "$defs": {key: json_schema(value) for key, value in schemas.items()},
    }
    return Draft202012Validator(schema)


def _schema(resource, data):
    if not isinstance(data, dict) or not _validator(resource).is_valid(data):
        raise PlayError("Account response does not match the pinned contract.")


def _user(data, developer):
    _schema("User", data)
    d, email, _ = target(data.get("name"))
    if d != developer or data.get("email") != email:
        raise PlayError("Account observation identity mismatch.")
    seen = set()
    for grant in data.get("grants", []):
        _grant(grant, developer, email)
        if grant["name"] in seen:
            raise PlayError("Account observation contains duplicate grants.")
        seen.add(grant["name"])


def _grant(data, developer, email):
    _schema("Grant", data)
    d, u, app = target(data.get("name"), grant=True)
    if (
        d != developer
        or u != email
        or data.get("packageName", "") != ("" if app.isdigit() else app)
    ):
        raise PlayError("Account grant identity mismatch.")


def validate_response(spec, data):
    if spec["method"] == READ_METHOD:
        _schema("ListUsersResponse", data)
        for user in data.get("users", []):
            _user(user, spec["developer"])
        token = data.get("nextPageToken")
        if token is not None and (
            not isinstance(token, str) or len(token) > 8192 or not token.isprintable() and token
        ):
            raise PlayError("Account response has an invalid pagination token.")
    elif spec["operation"] != "delete":
        if spec["grant"]:
            _grant(data, spec["developer"], spec["user"])
        else:
            _user(data, spec["developer"])
        if data["name"] != spec["resource"]:
            raise PlayError("Account mutation response identity mismatch.")


def fresh(deadline):
    if time.monotonic() >= deadline:
        raise PlayError("Account preparation expired; no further request attempted.")


def observe(spec, token, deadline, *, _execution_authorization=None, _max_snapshot_bytes=None):
    """Read every page to establish target presence/absence without returning other users."""
    limit = 10 * MAX_BYTES if _max_snapshot_bytes is None else _max_snapshot_bytes
    if type(limit) is not int or not 0 < limit <= 10 * MAX_BYTES:
        raise PlayError("Invalid account observation byte limit.")
    parameters = {"parent": "developers/" + spec["developer"], "pageSize": 100}
    seen_tokens, seen_users = set(), set()
    found = None
    total_bytes = 0
    for _ in range(MAX_PAGES):
        if _execution_authorization is not None:
            _execution_authorization()
        fresh(deadline)
        page_spec = request_spec(READ_METHOD, parameters)
        remaining = limit - total_bytes
        if remaining <= 0:
            raise PlayError("Complete account observation exceeds its bounded capacity.")
        options = (
            {
                "deadline": deadline,
                "_execution_authorization": _execution_authorization,
                "_max_bytes": min(MAX_BYTES, remaining),
            }
            if _execution_authorization is not None or _max_snapshot_bytes is not None
            else {}
        )
        page = request(page_spec, token, **options)
        if _execution_authorization is not None:
            _execution_authorization()
        fresh(deadline)
        validate_response(page_spec, page)
        total_bytes += len(json.dumps(page).encode())
        if total_bytes > limit:
            raise PlayError("Complete account observation exceeds its bounded capacity.")
        for user in page.get("users", []):
            if user["name"] in seen_users:
                raise PlayError("Complete account observation contains duplicate users.")
            seen_users.add(user["name"])
            if len(seen_users) > MAX_USERS:
                raise PlayError("Complete account observation exceeds its bounded capacity.")
            if user["email"] == spec["user"]:
                found = user
        next_token = page.get("nextPageToken")
        if not next_token:
            if (
                _max_snapshot_bytes is not None
                and total_bytes + len(json.dumps(found).encode()) > limit
            ):
                raise PlayError("Complete account observation and target preview exceed capacity.")
            fresh(deadline)
            return found
        if next_token in seen_tokens:
            raise PlayError("Account pagination repeated a token; baseline incomplete.")
        seen_tokens.add(next_token)
        parameters["pageToken"] = next_token
    raise PlayError("Complete account observation exceeded the page limit.")
