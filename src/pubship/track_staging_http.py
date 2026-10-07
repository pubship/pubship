"""Bounded track contracts and a fixed-route, single-attempt PUT transport."""

import json
import math
import re
import urllib.error
import urllib.request
from copy import deepcopy
from http.client import HTTPException

from .auth import validate_token
from .config import PACKAGE
from .contracts import validate_request
from .errors import PlayError
from .http import MAX_BYTES, NoRedirect, finite_float
from .play import path_segment
from .provider_transport import bounded_opener, dispatch_check

GET_METHOD = "androidpublisher.edits.tracks.get"
UPDATE_METHOD = "androidpublisher.edits.tracks.update"
UNKNOWN_OUTCOME = (
    "Track PUT outcome unknown; reconcile the caller-owned edit before preparing another "
    "operation. Do not automatically retry. This operation ID has been consumed."
)
RELEASE_FIELDS = {
    "name",
    "versionCodes",
    "releaseNotes",
    "status",
    "userFraction",
    "countryTargeting",
    "inAppUpdatePriority",
}
STATUSES = {"draft", "inProgress", "halted", "completed"}


def _unicode_json(value):
    """Reject non-JSON and lone surrogates, including unknown provider fields."""
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise PlayError("Track data must contain finite JSON and valid Unicode strings.") from None
    if len(encoded) > 64 * 1024:
        raise PlayError("Track data exceeds the 64 KiB limit.")


def _keys(value, allowed, required=(), *, request):
    if (
        not isinstance(value, dict)
        or not set(required) <= value.keys()
        or (request and not value.keys() <= allowed)
    ):
        raise PlayError("Unexpected or missing track release fields.")


def _releases(releases, *, request, track):
    if not isinstance(releases, list):
        raise PlayError("Releases must be an explicit array.")
    for release in releases:
        _keys(
            release, RELEASE_FIELDS, ("status", "versionCodes") if request else (), request=request
        )
        if not release:
            raise PlayError("Publisher returned an empty release record.")
        for key in ("name", "status"):
            if key in release and not isinstance(release[key], str):
                raise PlayError("Release name and status must be strings.")
        status = release.get("status")
        if request and status not in STATUSES:
            raise PlayError("Release status must be draft, inProgress, halted or completed.")
        if "versionCodes" in release:
            codes = release["versionCodes"]
            if not isinstance(codes, list) or any(not isinstance(v, str) for v in codes):
                raise PlayError("Version codes must be an array of decimal strings.")
            if (
                not codes
                or any(
                    not re.fullmatch(r"[1-9][0-9]{0,18}", v) or int(v) > 2**63 - 1 for v in codes
                )
                or (request and len(set(codes)) != len(codes))
            ):
                raise PlayError("Each release needs unique positive int64 version-code strings.")
        if "userFraction" in release:
            fraction = release["userFraction"]
            if type(fraction) not in (int, float) or (
                isinstance(fraction, float) and not math.isfinite(fraction)
            ):
                raise PlayError("userFraction must be a finite number.")
            if request and (not 0 < fraction < 1 or status not in {"inProgress", "halted"}):
                raise PlayError("userFraction must be between 0 and 1 for inProgress or halted.")
        elif request and status == "inProgress":
            raise PlayError("An inProgress proposal requires an explicit userFraction.")
        if "inAppUpdatePriority" in release:
            priority = release["inAppUpdatePriority"]
            if type(priority) is not int or (request and not 0 <= priority <= 5):
                raise PlayError("inAppUpdatePriority must be an integer from 0 through 5.")
        if "releaseNotes" in release:
            notes = release["releaseNotes"]
            if not isinstance(notes, list):
                raise PlayError("Release notes must be an array.")
            languages = []
            for note in notes:
                _keys(
                    note,
                    {"language", "text"},
                    ("language", "text") if request else (),
                    request=request,
                )
                if any(not isinstance(note[k], str) for k in ("language", "text") if k in note):
                    raise PlayError("Release note language and text must be strings.")
                if request:
                    path_segment(note["language"])
                    languages.append(note["language"])
            if request and len(set(languages)) != len(languages):
                raise PlayError("Release note languages must be unique within each release.")
        if "countryTargeting" in release:
            countries = release["countryTargeting"]
            _keys(countries, {"countries", "includeRestOfWorld"}, request=request)
            if "countries" in countries:
                codes = countries["countries"]
                if not isinstance(codes, list) or any(not isinstance(c, str) for c in codes):
                    raise PlayError("Countries must be an array of strings.")
                if request and (
                    any(not re.fullmatch(r"[A-Z]{2}", c) for c in codes)
                    or len(set(codes)) != len(codes)
                ):
                    raise PlayError("Countries must be unique uppercase two-letter codes.")
            if (
                "includeRestOfWorld" in countries
                and type(countries["includeRestOfWorld"]) is not bool
            ):
                raise PlayError("includeRestOfWorld must be a boolean.")
            if request and (
                status != "inProgress"
                or not (track == "production" or track.endswith(":production"))
            ):
                raise PlayError(
                    "Country targeting is supported only for inProgress production releases."
                )


def validate_track_request(parameters, releases):
    """Run all local validation before credential resolution or provider I/O."""
    if not isinstance(parameters, dict) or set(parameters) != {"packageName", "editId", "track"}:
        raise PlayError("Supply exactly packageName, editId and track as parameters.")
    for name, value in parameters.items():
        path_segment(
            value, limit={"packageName": 255, "editId": 1024}.get(name, 100), track=name == "track"
        )
    if not PACKAGE.fullmatch(parameters["packageName"]):
        raise PlayError("Invalid Android package name.")
    body = {"track": parameters["track"], "releases": releases}
    _unicode_json({"parameters": parameters, "body": body})
    validate_request(UPDATE_METHOD, parameters, body)
    _releases(releases, request=True, track=parameters["track"])


def validate_response(data, parameters):
    _unicode_json(data)
    if not isinstance(data, dict) or data.get("track") != parameters["track"]:
        raise PlayError("Publisher returned an unexpected track identity or shape.")
    if "releases" in data:
        _releases(data["releases"], request=False, track=parameters["track"])


def request_spec(parameters, releases):
    validate_track_request(parameters, releases)
    return {
        "method": UPDATE_METHOD,
        "http_method": "PUT",
        "url": (
            "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
            + parameters["packageName"]
            + "/edits/"
            + parameters["editId"]
            + "/tracks/"
            + path_segment(parameters["track"], track=True)
        ),
        "parameters": deepcopy(parameters),
        "body": {"track": parameters["track"], "releases": deepcopy(releases)},
    }


def execute(parameters, releases, token, *, deadline=None, _execution_authorization=None):
    """Only construct the reviewed track PUT; block redirects and never retry."""
    spec = request_spec(parameters, releases)
    req = urllib.request.Request(
        spec["url"],
        data=json.dumps(spec["body"], allow_nan=False).encode("utf-8"),
        method="PUT",
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
        data = json.loads(content, parse_float=finite_float, parse_constant=finite_float)
        validate_response(data, parameters)
        if releases and not data.get("releases"):
            raise PlayError(UNKNOWN_OUTCOME)
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
