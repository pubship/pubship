"""Fixed app-store contracts from pinned Discovery and provider-required intent."""

import json
import re
from copy import deepcopy
from datetime import UTC, date, datetime
from urllib.parse import quote, urlencode

from jsonschema import Draft202012Validator

from .config import PACKAGE
from .contracts import definitions, json_schema, methods, validate_request
from .errors import PlayError

PREFIX = "androidpublisher.appstoreappsreview."
CREATE = PREFIX + "createappstorehostedapp"
UPDATE = PREFIX + "updateappstorehostedapp"
STATUS = PREFIX + "updateappstorehostedapppublishstatus"
APK = PREFIX + "uploadapk"
IMAGE = PREFIX + "uploadimage"
POLICY = PREFIX + "uploadappstoreapppolicydeclarationfile"
GET = "androidpublisher.appstorecatalog.recentappviews.get"
LIST = "androidpublisher.appstorecatalog.recentupdateevents.list"
READ_METHODS = frozenset((GET, LIST))
UPLOAD_METHODS = frozenset((APK, IMAGE, POLICY))
WRITE_METHODS = frozenset((CREATE, UPDATE, STATUS)) | UPLOAD_METHODS
APPSTORE_METHODS = READ_METHODS | WRITE_METHODS
EFFECTS = {
    CREATE: "Creates the app's store-hosted record; required before other hosted-app operations.",
    UPDATE: "Submits complete app details, listings, active APK sets and operator-supplied policy declarations for review immediately. The default publish state after this update is PUBLISHED. This is not a draft or edit staging operation.",
    STATUS: "Reports the app's explicitly supplied published or unpublished state on the third-party store.",
    APK: "Uploads an APK and returns an APK ID. Does not verify package identity, signatures or installability locally.",
    IMAGE: "Uploads an icon or screenshot and returns an image ID.",
    POLICY: "Uploads an operator-supplied policy document with its explicit file type and returns a file ID. No declaration answers or compliance conclusions are inferred.",
}
MEDIA = {
    APK: ({"application/octet-stream", "application/vnd.android.package-archive"}, 10737418240),
    IMAGE: ({"image/png", "image/jpeg"}, 15728640),
    POLICY: ({"application/pdf", "image/png", "image/jpeg"}, 10485760),
}


def package(value):
    if not isinstance(value, str) or len(value) > 255 or not PACKAGE.fullmatch(value):
        raise PlayError("App-store scope requires an exact Android package name.")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 16384:
        raise PlayError("App-store intent requires explicit bounded nonempty text.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise PlayError("App-store text contains unsupported control characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PlayError("App-store text must be valid Unicode.") from None


def _intent(value, node, depth=0):
    if depth > 16:
        raise PlayError("App-store intent exceeds the nesting limit.")
    ref = node.get("$ref")
    if ref:
        node = definitions()["androidpublisher"]["schemas"][ref]
    if node.get("readOnly"):
        raise PlayError("Output-only fields cannot be submitted.")
    if isinstance(value, dict):
        properties = node.get("properties", {})
        required = {
            k for k, v in properties.items() if v.get("description", "").startswith("Required.")
        }
        if not required <= value.keys():
            raise PlayError("App-store intent is missing provider-required fields.")
        for k, v in value.items():
            _intent(v, properties.get(k, {}), depth + 1)
        if ref in {"PolicyResponse", "NestedPolicyResponse"}:
            if len(set(value) - {"questionId"}) != 1:
                raise PlayError("Each policy question requires exactly one explicit response type.")
        if ref == "PolicyDocumentResponse":
            if value.get("nonExpiring") is True and "expiryDate" in value:
                raise PlayError("A nonexpiring document cannot also have an expiry date.")
        if ref == "PolicyKeyedGroupResponse":
            keys = [v["key"] for v in value.get("groups", [])]
            if len(keys) != len(set(keys)):
                raise PlayError("Policy group keys must be unique.")
        if ref == "AppStoreAppActiveApkSet":
            if value.get("alreadyPublishedOnPlay") is True and "versionCode" not in value:
                raise PlayError("Already-published APK sets require versionCode.")
        if ref == "Date":
            try:
                date(value["year"], value["month"], value["day"])
            except (ValueError, TypeError, KeyError):
                raise PlayError(
                    "Document expiry requires a complete valid calendar date."
                ) from None
    elif isinstance(value, list):
        if len(value) > 1000 or (not value and node.get("description", "").startswith("Required.")):
            raise PlayError(
                "Required collections must be nonempty; collections are limited to 1000 items."
            )
        for item in value:
            _intent(item, node.get("items", {}), depth + 1)
        if node.get("items", {}).get("$ref") in {"PolicyResponse", "NestedPolicyResponse"}:
            ids = [item["questionId"] for item in value]
            if len(ids) != len(set(ids)):
                raise PlayError("Policy question IDs must be unique within their response group.")
    elif isinstance(value, str):
        _text(value)
        if node.get("format") == "int64" and (
            not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1
        ):
            raise PlayError("App-store version codes must be positive decimal int64 strings.")
    elif node.get("type") == "integer" and type(value) is not int:
        raise PlayError("Integer fields require JSON integers.")


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
        value,
    ):
        raise PlayError("Catalog time bounds require RFC3339 timestamps with timezone.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        seconds = parsed.replace(microsecond=0).astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        fraction = re.search(r"\.(\d{1,9})", value)
        nanos = int(fraction.group(1).ljust(9, "0")) if fraction else 0
        return (seconds.days * 86400 + seconds.seconds) * 10**9 + nanos
    except (ValueError, OverflowError):
        raise PlayError("Catalog time bounds require valid RFC3339 timestamps.") from None


def request_spec(method, parameters, body=None, mime_type=None):
    if not isinstance(method, str) or method not in APPSTORE_METHODS:
        raise PlayError("Method is outside the fixed app-store capability.")
    validate_request(method, parameters, body)
    definition = methods()[method]
    package(parameters["appStorePackageName"])
    target = parameters.get("packageName", parameters.get("playAppPackageName"))
    if method in (CREATE, UPDATE):
        target = body.get("packageName")
    if method != LIST:
        package(target)
    if method in WRITE_METHODS:
        _intent(body, definition["request"])
    if method == STATUS and body.get("publishState") not in {
        "APP_STORE_APP_PUBLISH_STATE_PUBLISHED",
        "APP_STORE_APP_PUBLISH_STATE_UNPUBLISHED",
    }:
        raise PlayError("An explicit published or unpublished state is required.")
    if method == POLICY and body.get("fileType") != "DECLARATION_FILE_TYPE_DOCUMENT":
        raise PlayError("Policy file upload requires explicit DOCUMENT fileType.")
    if method == UPDATE:
        languages = [v["languageCode"] for v in body["activeLocalizedStoreListings"]]
        declarations = [v["declarationId"] for v in body["policyDeclarations"]]
        if len(set(languages)) != len(languages) or len(set(declarations)) != len(declarations):
            raise PlayError("Listing languages and policy declaration IDs must be unique.")
    if method == LIST:
        if _timestamp(parameters.get("startTime")) >= _timestamp(parameters.get("endTime")):
            raise PlayError("Catalog startTime must precede exclusive endTime.")
        size = parameters.get("pageSize", 100)
        if type(size) is not int or not 1 <= size <= 1000:
            raise PlayError("Catalog pageSize must be an integer from 1 to 1000.")
        if "pageToken" in parameters:
            _text(parameters["pageToken"])
    path = definition["path"]
    query = {}
    if method in UPLOAD_METHODS:
        if mime_type not in MEDIA[method][0]:
            raise PlayError(
                "Unsupported app-store media type; images are limited locally to PNG/JPEG."
            )
        path = definition["mediaUpload"]["protocols"]["simple"]["path"].lstrip("/")
        query["uploadType"] = "multipart" if method == POLICY else "media"
    elif mime_type is not None:
        raise PlayError("mime_type is only accepted for app-store uploads.")
    for key, value in parameters.items():
        if definition["parameters"][key]["location"] == "path":
            path = path.replace("{" + key + "}", quote(value, safe=""))
        else:
            query[key] = value
    return {
        "method": method,
        "http_method": definition["httpMethod"],
        "url": "https://androidpublisher.googleapis.com/"
        + path
        + ("?" + urlencode(query) if query else ""),
        "parameters": deepcopy(parameters),
        "body": deepcopy(body),
        "target_package": target,
        "mime_type": mime_type,
        "effects": [EFFECTS[method]] if method in EFFECTS else [],
    }


def validate_response(spec, data):
    try:
        encoded = json.dumps(data, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PlayError("Invalid app-store response.") from None
    schema = json_schema(methods()[spec["method"]]["response"])
    schema["$defs"] = {
        k: json_schema(v) for k, v in definitions()["androidpublisher"]["schemas"].items()
    }
    if len(encoded) > 2 * 1024 * 1024 or not Draft202012Validator(schema).is_valid(data):
        raise PlayError("App-store response does not match its bounded pinned contract.")
    method = spec["method"]
    if method in UPLOAD_METHODS:
        _text(data.get({APK: "apkId", IMAGE: "imageId", POLICY: "fileId"}[method]))
    if method == GET and data.get("appView", {}).get("packageName") != spec["target_package"]:
        raise PlayError("Catalog returned an unexpected app identity.")
    if method == LIST:
        if len(data.get("recentUpdateEvents", [])) > spec["parameters"].get("pageSize", 100):
            raise PlayError("Catalog returned more events than requested.")
        for event in data.get("recentUpdateEvents", []):
            package(event.get("playAppPackageName"))
            event_time = _timestamp(event.get("eventTime"))
            if (
                not _timestamp(spec["parameters"]["startTime"])
                <= event_time
                < _timestamp(spec["parameters"]["endTime"])
            ):
                raise PlayError("Catalog event falls outside the requested time range.")
            if event.get("updateType") not in {"MODIFICATION", "DELETION"}:
                raise PlayError("Catalog returned an unspecified event type.")
