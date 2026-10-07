"""Narrow JSON edit-write contracts, full resource snapshots and explicit effects."""

import json
import re
from copy import deepcopy

from .config import PACKAGE
from .contracts import methods, validate_request
from .coverage import EDIT_WRITE_METHODS
from .errors import PlayError
from .listing_preview import listing_preview
from .play import path_segment
from .publishing import _json_snapshot
from .track_staging_http import validate_response as validate_track_response
from .track_staging_http import validate_track_request

PREFIX = "androidpublisher.edits."
DETAIL_FIELDS = {"contactEmail", "contactPhone", "contactWebsite", "defaultLanguage"}
LISTING_FIELDS = {"language", "title", "shortDescription", "fullDescription", "video"}
EFFECTS = {
    "details.patch": "Changes the supplied app detail fields, including default language or support contacts.",
    "details.update": "Replaces the intended app details, including default language and all support contacts.",
    "expansionfiles.patch": "Repoints this APK's expansion-file reference to another APK; no bytes are uploaded.",
    "expansionfiles.update": "Replaces this APK's expansion-file reference with another APK's reference; no bytes are uploaded.",
    "images.delete": "Deletes the specified image from this edit's locale and image type.",
    "images.deleteall": "Deletes the entire image collection for this edit's locale and image type.",
    "listings.delete": "Deletes the entire localized listing. The listing snapshot does not inventory associated imagery.",
    "listings.deleteall": "Deletes every localized listing in this edit. The listing snapshot does not inventory associated imagery.",
    "listings.update": "Creates or updates the entire intended localized listing; omitted video is not copied from the baseline.",
    "testers.patch": "Sends the complete intended Google Groups array, which can change tester access. Empty means intentional clearing; this is not an email membership list.",
    "testers.update": "Replaces the intended Google Groups array, which can change tester access. Empty means intentional clearing; this is not an email membership list.",
    "tracks.create": "Creates a closed testing track in the caller-owned edit; no releases are added.",
    "tracks.patch": "Sends the complete intended releases array for the track. Include all version codes to retain; no release merge keys or automatic appending are used.",
}


def bounded_json(value):
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise PlayError("Edit write data must contain finite JSON and valid Unicode.") from None
    return _json_snapshot(value)


def _string_fields(data, fields):
    if any(not isinstance(data[key], str) for key in fields if key in data):
        raise PlayError("Publisher returned an unexpected resource field type.")


def _positive_int(value):
    return type(value) is int and 1 <= value <= 2**31 - 1


def _url(method_id, parameters):
    path = methods()[method_id]["path"]
    for name, value in parameters.items():
        encoded = (
            str(value)
            if type(value) is int
            else path_segment(
                value,
                limit={"packageName": 255, "editId": 1024, "imageId": 1024}.get(name, 100),
                track=name == "track",
            )
        )
        path = path.replace("{" + name + "}", encoded)
    return "https://androidpublisher.googleapis.com/" + path


def request_spec(method_id, parameters, body=None):
    if not isinstance(method_id, str) or method_id not in EDIT_WRITE_METHODS:
        raise PlayError("Method is not a supported edit resource write.")
    bounded_json({"parameters": parameters, "body": body})
    validate_request(method_id, parameters, body)
    definition = methods()[method_id]
    if definition["httpMethod"] not in {"PATCH", "PUT", "POST", "DELETE"}:
        raise PlayError("Unsupported edit write contract.")
    if definition["httpMethod"] == "DELETE":
        if body is not None:
            raise PlayError("Delete requests must omit the body.")
    elif not isinstance(body, dict) or not body:
        raise PlayError("This edit write requires an explicit nonempty object body.")
    if not PACKAGE.fullmatch(parameters["packageName"]):
        raise PlayError("Invalid Android package name.")
    if "apkVersionCode" in parameters and not _positive_int(parameters["apkVersionCode"]):
        raise PlayError("apkVersionCode must be a positive JSON int32.")
    if (
        parameters.get("imageType") == "appImageTypeUnspecified"
        or parameters.get("expansionFileType") == "expansionFileTypeUnspecified"
    ):
        raise PlayError("Select a supported image or expansion file type.")
    name = method_id.removeprefix(PREFIX)
    if name.startswith("details."):
        if name == "details.update" and set(body) != DETAIL_FIELDS:
            raise PlayError("Details update requires all four app detail fields explicitly.")
        if "defaultLanguage" in body:
            path_segment(body["defaultLanguage"])
    elif name.startswith("expansionfiles."):
        if set(body) != {"referencesVersion"} or not _positive_int(body["referencesVersion"]):
            raise PlayError("Expansion writes require exactly a positive int32 referencesVersion.")
    elif name == "listings.update":
        if (
            not LISTING_FIELDS - {"video"} <= body.keys()
            or body["language"] != parameters["language"]
        ):
            raise PlayError(
                "Listing update requires matching language, title, shortDescription and fullDescription."
            )
    elif name.startswith("testers."):
        if set(body) != {"googleGroups"}:
            raise PlayError("Tester writes require the complete explicit googleGroups array.")
        groups = body["googleGroups"]
        if (
            len(groups) > 1000
            or len(set(groups)) != len(groups)
            or any(
                not 1 <= len(group) <= 320
                or not re.fullmatch(r"[^\s@]+@[^\s@]+", group)
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in group)
                for group in groups
            )
        ):
            raise PlayError(
                "Google Groups must be at most 1000 distinct bounded email-address strings."
            )
    elif name == "tracks.patch":
        if set(body) != {"track", "releases"} or body["track"] != parameters["track"]:
            raise PlayError("Track patch requires matching track and a complete releases array.")
        validate_track_request(parameters, body["releases"])
    elif name == "tracks.create":
        if set(body) != {"track", "type", "formFactor"} or body["type"] != "CLOSED_TESTING":
            raise PlayError("Track creation requires track, formFactor and type=CLOSED_TESTING.")
        track = body["track"]
        path_segment(track, track=True)
        prefix = {"DEFAULT": "", "WEAR": "wear:", "AUTOMOTIVE": "automotive:"}.get(
            body["formFactor"]
        )
        if (
            prefix is None
            or (not prefix and ":" in track)
            or (
                prefix
                and (
                    not track.startswith(prefix)
                    or ":" in track[len(prefix) :]
                    or not track[len(prefix) :]
                )
            )
        ):
            raise PlayError("Track prefix must match DEFAULT, WEAR or AUTOMOTIVE formFactor.")
    return {
        "method": method_id,
        "http_method": definition["httpMethod"],
        "url": _url(method_id, parameters),
        "parameters": deepcopy(parameters),
        "body": deepcopy(body),
    }


def baseline_spec(method_id, parameters):
    name = method_id.removeprefix(PREFIX)
    family = name.split(".")[0]
    if family in {"images", "listings"} or name == "tracks.create":
        baseline = PREFIX + family + ".list"
    else:
        baseline = PREFIX + family + ".get"
    keys = methods()[baseline]["parameters"]
    selected = {key: parameters[key] for key in keys}
    return {"method": baseline, "parameters": selected, "url": _url(baseline, selected)}


def _collection(data, field, identity, validate, *, required=True):
    rows = data.get(field) if required else data.get(field, [])
    if not isinstance(rows, list):
        raise PlayError("Publisher returned an unexpected collection shape.")
    identifiers = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(identity), str) or not row[identity]:
            raise PlayError("Publisher returned a resource without its identity.")
        validate(row)
        identifiers.append(row[identity])
    if len(set(identifiers)) != len(identifiers):
        raise PlayError("Publisher returned duplicate resource identities.")


def _image(data):
    _string_fields(data, {"id", "url", "sha1", "sha256", "aiGeneratedState"})


def _listing(data):
    listing_preview({"data": data, "fetched_at": ""}, {"language": data["language"]}, {})


def _testers(data):
    if "googleGroups" in data and (
        not isinstance(data["googleGroups"], list)
        or any(not isinstance(group, str) for group in data["googleGroups"])
    ):
        raise PlayError("Publisher returned an unexpected Google Groups array.")


def _expansion(data):
    if "referencesVersion" in data and not _positive_int(data["referencesVersion"]):
        raise PlayError("Publisher returned an unexpected expansion reference.")
    if "fileSize" in data and (
        not isinstance(data["fileSize"], str)
        or not re.fullmatch(r"0|[1-9][0-9]{0,18}", data["fileSize"])
        or int(data["fileSize"]) > 2**63 - 1
    ):
        raise PlayError("Publisher returned an unexpected expansion file size.")
    if {"fileSize", "referencesVersion"} <= data.keys():
        raise PlayError("Publisher returned conflicting expansion fields.")


def validate_baseline(method_id, parameters, data):
    bounded_json(data)
    if not isinstance(data, dict):
        raise PlayError("Publisher returned an unexpected edit resource shape.")
    _string_fields(data, {"kind"})
    name = method_id.removeprefix(PREFIX)
    if name.startswith("details."):
        _string_fields(data, DETAIL_FIELDS)
    elif name.startswith("expansionfiles."):
        _expansion(data)
    elif name.startswith("images."):
        _collection(data, "images", "id", _image)
    elif name.startswith("listings."):
        _collection(data, "listings", "language", _listing)
    elif name.startswith("testers."):
        _testers(data)
    elif name == "tracks.create":
        _collection(
            data,
            "tracks",
            "track",
            lambda row: validate_track_response(row, {"track": row["track"]}),
        )
    elif name == "tracks.patch":
        validate_track_response(data, parameters)


def require_creation_absent(method_id, body, baseline):
    if method_id == PREFIX + "tracks.create" and any(
        row["track"] == body["track"] for row in baseline.get("tracks", [])
    ):
        raise PlayError("The requested track already exists; no creation was attempted.")


def validate_response(method_id, parameters, body, data):
    bounded_json(data)
    if not isinstance(data, dict):
        raise PlayError("Publisher returned an unexpected edit write response.")
    name = method_id.removeprefix(PREFIX)
    if name in {"images.delete", "listings.delete", "listings.deleteall"}:
        if data:
            raise PlayError("Publisher returned an unexpected deletion response.")
    elif name == "images.deleteall":
        _collection(data, "deleted", "id", _image, required=False)
    elif name == "listings.update":
        listing_preview({"data": data, "fetched_at": ""}, parameters, {})
    elif name in {"tracks.create", "tracks.patch"}:
        validate_track_response(data, {"track": body["track"]})
    else:
        validate_baseline(method_id, parameters, data)


def observed_effect(method_id, parameters, body, after):
    name = method_id.removeprefix(PREFIX)
    if name.startswith("images.delete"):
        rows = after.get("images", [])
        absent = (
            not rows
            if name.endswith("deleteall")
            else all(row["id"] != parameters["imageId"] for row in rows)
        )
        return {"deletion_observed": absent}
    if name.startswith("listings.delete"):
        rows = after.get("listings", [])
        absent = (
            not rows
            if name.endswith("deleteall")
            else all(row["language"] != parameters["language"] for row in rows)
        )
        return {"deletion_observed": absent}
    if name == "tracks.create":
        return {
            "creation_observed": any(
                row["track"] == body["track"] for row in after.get("tracks", [])
            )
        }
    return {}
