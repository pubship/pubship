"""Pinned artifact routes and deliberately bounded local payload contracts."""

import base64
import binascii
import json
import re
from copy import deepcopy
from urllib.parse import quote, urlencode, urlsplit

from jsonschema import Draft202012Validator

from .config import PACKAGE
from .contracts import methods, request_schema, validate_request
from .coverage import ARTIFACT_METHODS
from .edit_writes_contracts import bounded_json
from .errors import PlayError
from .play import path_segment

PREFIX = "androidpublisher.edits."
EXTERNAL = PREFIX + "apks.addexternallyhosted"
DOWNLOAD_METHODS = frozenset(m for m in ARTIFACT_METHODS if m.endswith(".download"))
UPLOAD_METHODS = ARTIFACT_METHODS - DOWNLOAD_METHODS
CAPS = {
    PREFIX + "apks.upload": 10737418240,
    PREFIX + "bundles.upload": 53687091200,
    PREFIX + "deobfuscationfiles.upload": 2097152000,
    PREFIX + "expansionfiles.upload": 2147483648,
    PREFIX + "images.upload": 15728640,
}
EFFECTS = (
    "Stages the supplied artifact in the caller-owned edit, potentially replacing existing "
    "artifact content. No automatic edit creation, commit, track assignment, retry or rollback. "
    "Deobfuscation content has no readable baseline: only the target APK can be checked. "
    "External APK registration is for Managed Play with organization-restricted distribution; "
    "remote bytes are neither fetched nor verified by this server."
)


def int32(value):
    return type(value) is int and 1 <= value <= 2**31 - 1


def int64(value):
    return (
        isinstance(value, str)
        and bool(re.fullmatch(r"[1-9][0-9]{0,18}", value))
        and int(value) <= 2**63 - 1
    )


def _base64(value, size=None):
    try:
        data = base64.b64decode(value, validate=True) if isinstance(value, str) else b""
    except (ValueError, binascii.Error):
        data = b""
    if not data or (size is not None and len(data) != size):
        raise PlayError("External APK metadata contains invalid base64 or checksum length.")


def external_body(body, package):
    try:
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise PlayError("Artifact metadata must be finite JSON and valid Unicode.") from None
    if len(raw) > 8 * 1024 * 1024:
        raise PlayError("External APK metadata exceeds the local 8 MiB limit.")
    if not isinstance(body, dict) or set(body) != {"externallyHostedApk"}:
        raise PlayError("External APK registration requires exactly externallyHostedApk.")
    apk = body["externallyHostedApk"]
    required = {
        "packageName",
        "applicationLabel",
        "versionCode",
        "versionName",
        "fileSize",
        "fileSha1Base64",
        "fileSha256Base64",
        "iconBase64",
        "minimumSdk",
        "certificateBase64s",
        "externallyHostedUrl",
        "usesPermissions",
    }
    if (
        not isinstance(apk, dict)
        or not required <= apk.keys()
        or apk.keys() - required - {"maximumSdk", "nativeCodes", "usesFeatures"}
    ):
        raise PlayError("External APK metadata requires the complete supported declaration.")
    if (
        apk["packageName"] != package
        or not int32(apk["versionCode"])
        or not int32(apk["minimumSdk"])
    ):
        raise PlayError("External APK package or version/SDK identity is invalid.")
    if "maximumSdk" in apk and (
        not int32(apk["maximumSdk"]) or apk["maximumSdk"] < apk["minimumSdk"]
    ):
        raise PlayError("External APK maximum SDK must be at least its minimum SDK.")
    if not int64(apk["fileSize"]):
        raise PlayError("External APK fileSize must be a positive decimal int64 string.")
    if any(
        not isinstance(apk[k], str)
        for k in ("applicationLabel", "versionName", "externallyHostedUrl")
    ):
        raise PlayError("External APK text fields must be strings.")
    _base64(apk["fileSha1Base64"], 20)
    _base64(apk["fileSha256Base64"], 32)
    _base64(apk["iconBase64"])
    for key in ("certificateBase64s", "nativeCodes", "usesFeatures", "usesPermissions"):
        if key not in apk:
            continue
        values = apk[key]
        if not isinstance(values, list) or len(values) > 1000:
            raise PlayError("External APK arrays must contain at most 1000 entries.")
        if key == "certificateBase64s":
            if not values:
                raise PlayError("External APK certificates must be explicitly supplied.")
            for value in values:
                _base64(value)
        elif key == "usesPermissions":
            for value in values:
                if (
                    not isinstance(value, dict)
                    or not {"name"} <= value.keys()
                    or value.keys() - {"name", "maxSdkVersion"}
                    or not isinstance(value["name"], str)
                    or not value["name"]
                    or ("maxSdkVersion" in value and not int32(value["maxSdkVersion"]))
                ):
                    raise PlayError("External APK permission metadata is invalid.")
        elif any(not isinstance(value, str) or not value for value in values):
            raise PlayError("External APK feature and native-code entries must be strings.")
    url = apk["externallyHostedUrl"]
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.fragment
            and not any(ord(c) <= 32 or 127 <= ord(c) <= 159 for c in url)
            and "\\" not in url
            and re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
        )
    except ValueError:
        valid = False
    if not valid:
        raise PlayError(
            "External APK URL must use HTTPS, a hostname and port 443 without credentials or fragments."
        )


def request_spec(
    method, parameters, *, filename=None, mime_type=None, body=None, _media_source=False
):
    if not isinstance(method, str) or method not in ARTIFACT_METHODS:
        raise PlayError("Method is not a supported artifact operation.")
    bounded_json(parameters)
    if method == EXTERNAL:
        if filename is not None or mime_type is not None or _media_source:
            raise PlayError("External APK registration forbids a local filename or MIME type.")
        if not isinstance(parameters, dict):
            raise PlayError("Artifact parameters must be an object.")
        external_body(body, parameters.get("packageName"))
    elif body is not None:
        raise PlayError("This artifact method does not accept a JSON body.")
    # Discovery validation is deliberately applied after the large external body bound.
    if method == EXTERNAL:
        if not Draft202012Validator(request_schema(method)).is_valid(
            {"parameters": parameters, "body": body}
        ):
            raise PlayError("External artifact request does not match the pinned contract.")
    else:
        validate_request(method, parameters, body)
    if not PACKAGE.fullmatch(parameters["packageName"]):
        raise PlayError("Invalid Android package name.")
    definition = methods()[method]
    query, encoded = {}, {}
    for name, value in parameters.items():
        if name in {"apkVersionCode"} or (
            name == "versionCode" and method.endswith("generatedapks.download")
        ):
            if not int32(value):
                raise PlayError("Artifact version code must be a positive JSON int32.")
            encoded[name] = str(value)
        elif name == "variantId":
            if type(value) is not int or not 0 <= value <= 2**32 - 1:
                raise PlayError("Artifact variantId must be a JSON uint32.")
            encoded[name] = str(value)
        elif name == "versionCode":
            if not int64(value):
                raise PlayError("System APK versionCode must be a positive decimal int64 string.")
            encoded[name] = value
        elif name == "downloadId":
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 4096
                or any(not c.isprintable() for c in value)
                or "\\" in value
                or "%" in value
                or any(part in {".", ".."} for part in value.split("/"))
            ):
                raise PlayError("Invalid bounded opaque artifact download identifier.")
            encoded[name] = quote(value, safe="")
        elif definition["parameters"][name]["location"] == "query":
            if name == "deviceTierConfigId" and value != "LATEST" and not int64(value):
                raise PlayError(
                    "deviceTierConfigId must be LATEST or a positive decimal int64 string."
                )
            query[name] = str(value).lower() if type(value) is bool else value
        else:
            encoded[name] = path_segment(
                value, limit={"packageName": 255, "editId": 1024}.get(name, 100)
            )
    if (
        parameters.get("imageType") == "appImageTypeUnspecified"
        or parameters.get("expansionFileType") == "expansionFileTypeUnspecified"
        or parameters.get("deobfuscationFileType") == "deobfuscationFileTypeUnspecified"
    ):
        raise PlayError("Select an explicit supported artifact type.")
    if method in CAPS:
        if _media_source and filename is not None:
            raise PlayError("A trusted media source cannot also use a local filename.")
        if not _media_source and (not isinstance(filename, str) or not filename):
            raise PlayError("A local source filename is required for binary uploads.")
        if not isinstance(mime_type, str):
            raise PlayError("An explicit supported MIME type is required.")
        permitted = {"application/octet-stream"}
        if method.endswith("apks.upload"):
            permitted.add("application/vnd.android.package-archive")
        valid_mime = (
            bool(re.fullmatch(r"image/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*", mime_type))
            if method.endswith("images.upload")
            else mime_type in permitted
        )
        if not valid_mime:
            raise PlayError("MIME type is unsupported for this artifact method.")
        path = definition["mediaUpload"]["protocols"]["simple"]["path"]
        query["uploadType"] = "media"
    else:
        if method in DOWNLOAD_METHODS and (
            filename is not None or mime_type is not None or _media_source
        ):
            raise PlayError("Downloads use a server-generated destination filename.")
        path = "/" + definition["path"]
        if method in DOWNLOAD_METHODS:
            if definition.get("useMediaDownloadService") is not True:
                raise PlayError("Unsupported media download contract.")
            path = "/download" + path
            query["alt"] = "media"
    for name, value in encoded.items():
        path = path.replace("{" + name + "}", value)
    return {
        "method": method,
        "http_method": definition["httpMethod"],
        "url": "https://androidpublisher.googleapis.com"
        + path
        + ("?" + urlencode(query) if query else ""),
        "parameters": deepcopy(parameters),
        "mime_type": mime_type,
    }


def baseline_specs(method, parameters):
    base = {k: parameters[k] for k in ("packageName", "editId")}
    if method.endswith("bundles.upload"):
        reads = [("bundles.list", base, "bundles", "versionCode")]
    elif method.endswith("deobfuscationfiles.upload"):
        reads = [
            ("apks.list", base, "apks", "versionCode"),
            ("bundles.list", base, "bundles", "versionCode"),
        ]
    elif method.endswith("expansionfiles.upload"):
        reads = [
            (
                "expansionfiles.get",
                {**base, **{k: parameters[k] for k in ("apkVersionCode", "expansionFileType")}},
                None,
                None,
            )
        ]
    elif method.endswith("images.upload"):
        reads = [
            (
                "images.list",
                {**base, **{k: parameters[k] for k in ("language", "imageType")}},
                "images",
                "id",
            ),
            ("listings.list", base, "listings", "language"),
        ]
    else:
        reads = [("apks.list", base, "apks", "versionCode")]
    results = []
    for name, params, collection, identity in reads:
        path = methods()[PREFIX + name]["path"]
        for key, value in params.items():
            path = path.replace("{" + key + "}", quote(str(value), safe=""))
        results.append(
            {
                "url": "https://androidpublisher.googleapis.com/" + path,
                "collection": collection,
                "identity": identity,
            }
        )
    return results


def validate_baseline(spec, data):
    bounded_json(data)
    if not isinstance(data, dict):
        raise PlayError("Publisher returned malformed artifact metadata.")
    key, identity = spec["collection"], spec["identity"]
    if key is None:
        if "fileSize" in data and not int64(data["fileSize"]):
            raise PlayError("Publisher returned malformed expansion metadata.")
        if "referencesVersion" in data and not int32(data["referencesVersion"]):
            raise PlayError("Publisher returned malformed expansion metadata.")
        return True
    if key not in data:
        return False
    items = data[key]
    if not isinstance(items, list):
        raise PlayError("Publisher returned a malformed artifact collection.")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or identity not in item:
            raise PlayError("Publisher returned an artifact without an identity.")
        value = item[identity]
        if (
            not int32(value)
            if identity == "versionCode"
            else not isinstance(value, str) or not value
        ):
            raise PlayError("Publisher returned an invalid artifact identity.")
        if value in seen:
            raise PlayError("Publisher returned duplicate artifact identities.")
        seen.add(value)
    return True


def require_target(method, parameters, body, snapshots):
    if method.endswith("images.upload"):
        if not any(
            x["language"] == parameters["language"] for x in snapshots[1].get("listings", [])
        ):
            raise PlayError("Image upload requires an observed existing locale listing.")
    if method.endswith("deobfuscationfiles.upload"):
        if not any(
            item["versionCode"] == parameters["apkVersionCode"]
            for snapshot, key in zip(snapshots, ("apks", "bundles"), strict=True)
            for item in snapshot.get(key, [])
        ):
            raise PlayError(
                "Deobfuscation upload requires an observed existing APK or bundle version."
            )
    if method == EXTERNAL:
        if any(
            x["versionCode"] == body["externallyHostedApk"]["versionCode"]
            for x in snapshots[0].get("apks", [])
        ):
            raise PlayError("The external APK version is already present in the observed edit.")


def validate_response(method, parameters, body, snapshot, data):
    # External responses may echo an explicitly bounded large declaration.
    if method == EXTERNAL:
        try:
            if (
                len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode())
                > 8 * 1024 * 1024
            ):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise PlayError("Invalid artifact response.") from None
    else:
        bounded_json(data)
    if not isinstance(data, dict):
        raise PlayError("Invalid artifact response.")
    digest_verified = False
    if method.endswith(("apks.upload", "bundles.upload")):
        if not int32(data.get("versionCode")):
            raise PlayError("Artifact response has no valid version identity.")
        hashes = data.get("binary", {}) if method.endswith("apks.upload") else data
        if not isinstance(hashes, dict):
            raise PlayError("Artifact response has malformed binary metadata.")
        for name, length in (("sha1", 40), ("sha256", 64)):
            if name in hashes:
                value = hashes[name]
                if (
                    not isinstance(value, str)
                    or not re.fullmatch(r"[0-9a-fA-F]{" + str(length) + "}", value)
                    or value.lower() != getattr(snapshot, name)
                ):
                    raise PlayError(
                        "Artifact response checksum does not match the supplied snapshot."
                    )
                digest_verified = True
    elif method.endswith("deobfuscationfiles.upload"):
        value = data.get("deobfuscationFile")
        if (
            not isinstance(value, dict)
            or value.get("symbolType") != parameters["deobfuscationFileType"]
        ):
            raise PlayError("Artifact response has no matching symbol type.")
    elif method.endswith("expansionfiles.upload"):
        value = data.get("expansionFile")
        if (
            not isinstance(value, dict)
            or not int64(value.get("fileSize"))
            or int(value["fileSize"]) != snapshot.size
        ):
            raise PlayError("Expansion response size does not match uploaded bytes.")
    elif method.endswith("images.upload"):
        value = data.get("image")
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not value["id"]
            or any(not isinstance(value[k], str) for k in ("sha1", "sha256", "url") if k in value)
        ):
            raise PlayError("Image response has invalid identity or metadata.")
    elif method == EXTERNAL:
        value, expected = data.get("externallyHostedApk"), body["externallyHostedApk"]
        if (
            not isinstance(value, dict)
            or value.get("packageName") != parameters["packageName"]
            or type(value.get("versionCode")) is not int
            or value["versionCode"] != expected["versionCode"]
        ):
            raise PlayError("External APK response has no matching identity.")
        for key in ("fileSize", "fileSha1Base64", "fileSha256Base64", "externallyHostedUrl"):
            if key in value and value[key] != expected[key]:
                raise PlayError("External APK response conflicts with declared metadata.")
    return digest_verified
