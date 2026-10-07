"""Fixed support routes, bounded one-attempt writes and scoped observations."""

import json
import re
import time
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime
from http.client import HTTPException
from urllib.parse import quote, urlsplit

from jsonschema import Draft202012Validator

from .auth import validate_token
from .contracts import definitions, json_schema
from .errors import PlayError
from .http import NoRedirect, finite_float
from .provider_transport import bounded_opener, dispatch_check
from .publisher_reads_contracts import validate_publisher_read
from .support_contracts import (
    request_spec,
    validate_mutation_response,
    validate_targeting,
    validate_timestamp,
)

PREFIX = "androidpublisher."
REPLY = PREFIX + "reviews.reply"
RECOVERY = PREFIX + "apprecovery."
EXISTING_RECOVERY = frozenset(RECOVERY + op for op in ("addTargeting", "cancel", "deploy"))
MAX_BYTES = 2 * 1024 * 1024
UNKNOWN_OUTCOME = (
    "Support mutation outcome unknown; reconcile the target before preparing another operation. "
    "Do not automatically retry. The operation ID has been consumed."
)


def _request(
    url, token, *, body=None, empty_success=False, deadline=None, _execution_authorization=None
):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "androidpublisher.googleapis.com"
        or parsed.fragment
        or not parsed.path.startswith("/androidpublisher/v3/applications/")
    ):
        raise PlayError("Support requests require fixed Google Publisher application routes.")
    # URLs are built exclusively from validated fixed method contracts below.
    mutation = body is not None
    request = urllib.request.Request(
        url,
        method="POST" if mutation else "GET",
        data=json.dumps(body, allow_nan=False).encode() if mutation else None,
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
            raise ValueError("Unexpected response")
        if status == 204:
            if content or not empty_success:
                raise ValueError("Missing required representation")
            return {}
        if empty_success and not content.strip():
            return {}
        data = json.loads(content, parse_float=finite_float, parse_constant=finite_float)
        if not isinstance(data, dict):
            raise ValueError("Expected object")
        return data
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise PlayError(
            f"Google API HTTP {status}. "
            + (UNKNOWN_OUTCOME if mutation else "Support observation unavailable.")
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
            UNKNOWN_OUTCOME if mutation else "Support observation failed or returned invalid data."
        ) from None


def execute(method, parameters, body, token, *, deadline=None, _execution_authorization=None):
    spec = request_spec(method, parameters, body)
    if spec["http_method"] != "POST":
        raise PlayError("Only the fixed support POST methods are implemented.")
    data = _request(
        spec["url"],
        token,
        body=spec["body"],
        empty_success=spec.get("empty_response", False),
        deadline=deadline,
        _execution_authorization=_execution_authorization,
    )
    try:
        validate_mutation_response(spec, data)
    except PlayError:
        raise PlayError(UNKNOWN_OUTCOME) from None
    return data


def _resource(name, data):
    schemas = definitions()["androidpublisher"]["schemas"]
    schema = {
        "$ref": "#/$defs/" + name,
        "$defs": {key: json_schema(value) for key, value in schemas.items()},
    }
    if not isinstance(data, dict) or not Draft202012Validator(schema).is_valid(data):
        raise PlayError("Support observation does not match the pinned resource contract.")


def _version(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
        or int(value) > 2**63 - 1
    ):
        raise PlayError("observation_version_code must be a positive decimal int64 string.")
    return int(value)


def validate_observation_version(spec, version):
    if spec["method"] in EXISTING_RECOVERY:
        _version(version)
    elif version is not None:
        raise PlayError("observation_version_code is only used for existing recovery actions.")


def _target_version(targeting, version):
    validate_targeting(targeting)
    if not isinstance(targeting, dict):
        raise PlayError("Recovery observation lacks explicit targeting.")
    fields = set(targeting)
    if (
        len(fields & {"versionList", "versionRange"}) != 1
        or len(fields & {"regions", "androidSdks", "allUsers"}) != 1
    ):
        raise PlayError("Recovery observation has ambiguous targeting.")
    number = _version(version)
    if "versionList" in targeting:
        values = targeting["versionList"].get("versionCodes")
        if not isinstance(values, list) or not values or len(values) > 1000:
            raise PlayError("Recovery observation lacks a bounded version list.")
        contained = number in [_version(v) for v in values]
    else:
        bounds = targeting["versionRange"]
        contained = (
            _version(bounds.get("versionCodeStart"))
            <= number
            <= _version(bounds.get("versionCodeEnd"))
        )
    if not contained:
        raise PlayError("Recovery observation does not target the supplied version.")


def _bounded(value, *, max_bytes=None):
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode()) > (MAX_BYTES if max_bytes is None else max_bytes):
            raise ValueError("Snapshot too large")
        return encoded
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise PlayError(
            "Support observation must be finite JSON within "
            + ("2 MiB" if max_bytes is None else "the hosted preparation limit")
            + "."
        ) from None


def _observe_get(url, token, *, deadline, _execution_authorization, _max_snapshot_bytes):
    if _execution_authorization is not None:
        _execution_authorization()
    if deadline is not None and time.monotonic() >= deadline:
        raise PlayError("Support preparation expired; no further observation request attempted.")
    data = _request(url, token)
    if _max_snapshot_bytes is not None:
        _bounded(data, max_bytes=_max_snapshot_bytes)
    return data


def _recovery(spec, token, version, action_id, **read_options):
    parameters = {"packageName": spec["parameters"]["packageName"], "versionCode": version}
    _, url = validate_publisher_read(RECOVERY + "list", parameters)
    data = _observe_get(url, token, **read_options)
    if set(data) != {"recoveryActions"}:
        raise PlayError("Recovery list is missing or has an unsupported continuation/shape.")
    actions = data["recoveryActions"]
    if (
        not isinstance(actions, list)
        or len(actions) > 1000
        or any(not isinstance(a, dict) for a in actions)
    ):
        raise PlayError("Recovery observation must be a bounded complete list.")
    matches = [a for a in actions if a.get("appRecoveryId") == action_id]
    if len(matches) != 1:
        raise PlayError("Recovery observation requires exactly one matching action.")
    action = matches[0]
    _resource("AppRecoveryAction", action)
    if not action.get("lastUpdateTime") or not action.get("status"):
        raise PlayError("Recovery observation lacks status or update timing.")
    try:
        timestamp = datetime.fromisoformat(action["lastUpdateTime"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None or "T" not in action["lastUpdateTime"]:
            raise ValueError("Missing timezone")
    except (ValueError, TypeError):
        raise PlayError("Recovery observation has invalid update timing.") from None
    _target_version(action.get("targeting"), version)
    return action


def observe(
    spec,
    token,
    observation_version_code=None,
    mutation_response=None,
    *,
    deadline=None,
    _execution_authorization=None,
    _max_snapshot_bytes=None,
):
    read_options = {
        "deadline": deadline,
        "_execution_authorization": _execution_authorization,
        "_max_snapshot_bytes": _max_snapshot_bytes,
    }
    method, p = spec["method"], spec["parameters"]
    if method == REPLY:
        url = (
            "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
            + quote(p["packageName"], safe="")
            + "/reviews/"
            + quote(p["reviewId"], safe="")
        )
        data = _observe_get(url, token, **read_options)
        _resource("Review", data)
        if (
            data.get("reviewId") != p["reviewId"]
            or not isinstance(data.get("comments"), list)
            or not 1 <= len(data["comments"]) <= 1000
        ):
            raise PlayError("Review observation lacks the expected identity and conversation.")
        for comment in data["comments"]:
            if len(comment) != 1 or not set(comment) <= {"userComment", "developerComment"}:
                raise PlayError("Review observation has an ambiguous comment.")
            value = next(iter(comment.values()))
            if (
                not isinstance(value.get("text"), str)
                or not isinstance(value.get("lastModified"), dict)
                or "seconds" not in value["lastModified"]
            ):
                raise PlayError("Review observation lacks text or update timing.")
            validate_timestamp(value["lastModified"])
        return data
    if method in EXISTING_RECOVERY:
        return _recovery(spec, token, observation_version_code, p["appRecoveryId"], **read_options)
    if mutation_response is None:
        return None
    if method == RECOVERY + "create":
        targeting = spec["body"]["targeting"]
        version = (
            targeting["versionList"]["versionCodes"][0]
            if "versionList" in targeting
            else targeting["versionRange"]["versionCodeStart"]
        )
        return _recovery(spec, token, version, mutation_response["appRecoveryId"], **read_options)
    if method == PREFIX + "applications.deviceTierConfigs.create":
        identity = mutation_response["deviceTierConfigId"]
        read_method = PREFIX + "applications.deviceTierConfigs.get"
        params = {"packageName": p["packageName"], "deviceTierConfigId": identity}
        schema, field = "DeviceTierConfig", "deviceTierConfigId"
    elif method == PREFIX + "systemapks.variants.create":
        identity = mutation_response["variantId"]
        read_method = PREFIX + "systemapks.variants.get"
        params = {**p, "variantId": identity}
        schema, field = "Variant", "variantId"
    else:
        return None
    _, url = validate_publisher_read(read_method, params)
    data = _observe_get(url, token, **read_options)
    _resource(schema, data)
    if data.get(field) != identity:
        raise PlayError("Support readback returned a different resource identity.")
    return data


def check_preflight(spec, observation):
    method = spec["method"]
    if method not in EXISTING_RECOVERY:
        return
    status = observation["status"]
    if method.endswith(".deploy") and status != "RECOVERY_STATUS_DRAFT":
        raise PlayError("Only an observed draft recovery action may be deployed.")
    if method.endswith(".cancel") and status != "RECOVERY_STATUS_ACTIVE":
        raise PlayError("Only an observed active recovery action may be canceled.")
    if method.endswith(".addTargeting"):
        if status not in {"RECOVERY_STATUS_DRAFT", "RECOVERY_STATUS_ACTIVE"}:
            raise PlayError("Target expansion requires an observed draft or active action.")
        criteria = set(spec["body"]["targetingUpdate"])
        original = set(observation["targeting"])
        if "allUsers" in original:
            raise PlayError("An all-users recovery target cannot be expanded.")
        if criteria != {"allUsers"} and not criteria <= original:
            raise PlayError("Only the original recovery criterion or all users may be targeted.")


def fingerprint(spec, observation, *, max_bytes=None):
    if observation is None:
        return None
    value = deepcopy(observation)
    if spec["method"] == REPLY:
        for comment in value["comments"]:
            user = comment.get("userComment", {})
            user.pop("thumbsUpCount", None)
            user.pop("thumbsDownCount", None)
    elif spec["method"].startswith(RECOVERY):
        value.pop("remoteInAppUpdateData", None)
    return _bounded(value, max_bytes=max_bytes)
