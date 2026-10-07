"""Fixed contracts for operator-authored support and app configuration writes.

Google's pinned schemas validate shape; explicit checks below cover required
intent, output-only fields and unions omitted by Discovery's schema dialect.
"""

import json
import re
from copy import deepcopy
from urllib.parse import quote, urlencode

from jsonschema import Draft202012Validator

from .config import PACKAGE
from .contracts import definitions, json_schema, methods, validate_request
from .coverage import SUPPORT_METHODS
from .errors import PlayError
from .play import path_segment

PREFIX = "androidpublisher."
EFFECTS = {
    "reviews.reply": "Publishes or replaces the developer reply to this review. Google strips HTML and applies an approximately 350-character limit.",
    "applications.dataSafety": "Writes the app's Data safety declaration from the operator-supplied CSV. No privacy answers are inferred; Google validates the declaration. There is no corresponding Data safety read API.",
    "applications.deviceTierConfigs.create": "Creates a new device targeting configuration from the supplied groups, tiers and country sets. The generated configuration ID is assigned by Google.",
    "apprecovery.create": "Creates a draft remote in-app update recovery action and its testing module. Creating a draft does not deploy it to users.",
    "apprecovery.addTargeting": "Expands the recovery action's targeted users. Google requires the original criterion type unless expanding to all users; an all-users target cannot be expanded.",
    "apprecovery.cancel": "Cancels the executing recovery action. A canceled action cannot be resumed.",
    "apprecovery.deploy": "Deploys the draft recovery action to ALL its targeted users and makes it active.",
    "systemapks.variants.create": "Generates a system-image APK variant from the specified already uploaded app bundle and device specification. This does not upload or publish an app bundle.",
}
EMPTY_RESPONSE_METHODS = frozenset(
    PREFIX + name
    for name in (
        "applications.dataSafety",
        "apprecovery.addTargeting",
        "apprecovery.cancel",
        "apprecovery.deploy",
    )
)


def _text(value, *, limit=4096):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(c) < 32 and c not in "\n\r\t" for c in value)
    ):
        raise PlayError("Supply explicit nonempty bounded text without control characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PlayError("Support write text must be valid Unicode.") from None


def _int64(value, *, minimum=1):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"0|[1-9][0-9]{0,18}", value)
        or not minimum <= int(value) <= 2**63 - 1
    ):
        raise PlayError("Supply a canonical decimal int64 string in the permitted range.")
    return int(value)


def _distinct(values):
    if not isinstance(values, list) or not values or len(set(values)) != len(values):
        raise PlayError("Supply a nonempty array of distinct values.")


def _countries(values):
    _distinct(values)
    if any(not re.fullmatch(r"[A-Z]{2}", value) for value in values):
        raise PlayError("Country codes must be uppercase ISO 3166 alpha-2 codes.")


def _write_fields(value, node):
    if node.get("readOnly"):
        raise PlayError("Output-only fields cannot be supplied as support write intent.")
    if "$ref" in node:
        node = definitions()["androidpublisher"]["schemas"][node["$ref"]]
    if isinstance(value, dict):
        for key, child in value.items():
            _write_fields(child, node.get("properties", {}).get(key, {}))
    elif isinstance(value, list):
        for child in value:
            _write_fields(child, node.get("items", {}))
    elif node.get("type") == "integer" and type(value) is not int:
        raise PlayError("Integer fields require JSON integers, not booleans or floats.")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise PlayError("Support write text must be valid Unicode.") from None


def validate_targeting(targeting, *, update=False):
    """Validate both operator intent and provider-observed recovery targeting."""
    schemas = definitions()["androidpublisher"]["schemas"]
    schema = {
        "$ref": "#/$defs/TargetingUpdate" if update else "#/$defs/Targeting",
        "$defs": {key: json_schema(value) for key, value in schemas.items()},
    }
    if not Draft202012Validator(schema).is_valid(targeting):
        raise PlayError("Recovery targeting does not match the pinned contract.")
    if not isinstance(targeting, dict):
        raise PlayError("Explicit recovery targeting is required.")
    criteria = {"allUsers", "regions", "androidSdks"} & targeting.keys()
    if len(criteria) != 1:
        raise PlayError("Choose exactly one explicit recovery audience criterion.")
    criterion = next(iter(criteria))
    value = targeting[criterion]
    if criterion == "allUsers":
        if value.get("isAllUsersRequested") is not True:
            raise PlayError("All-users targeting requires isAllUsersRequested=true.")
    elif criterion == "regions":
        _countries(value.get("regionCode"))
    else:
        levels = value.get("sdkLevels")
        _distinct(levels)
        for level in levels:
            _int64(level)
    if update:
        return
    versions = {"versionList", "versionRange"} & targeting.keys()
    if len(versions) != 1:
        raise PlayError("Choose exactly one explicit recovery version list or range.")
    if "versionList" in targeting:
        codes = targeting["versionList"].get("versionCodes")
        _distinct(codes)
        for code in codes:
            _int64(code)
    else:
        bounds = targeting["versionRange"]
        if _int64(bounds.get("versionCodeStart")) > _int64(bounds.get("versionCodeEnd")):
            raise PlayError("Recovery version range must be ordered inclusively.")


def _device_config(body):
    if not body or not any(body.values()):
        raise PlayError("Supply an explicit nonempty device targeting configuration.")
    groups = body.get("deviceGroups", [])
    names = []
    for group in groups:
        _text(group.get("name"))
        names.append(group["name"])
        selectors = group.get("deviceSelectors")
        if not selectors:
            raise PlayError("Device groups require explicit device selectors.")
        for selector in selectors:
            if not selector or not any(selector.values()):
                raise PlayError("Device selectors require explicit conditions.")
            if "deviceRam" in selector:
                ram = selector["deviceRam"]
                if not ram:
                    raise PlayError("RAM targeting requires an explicit bound.")
                low = _int64(ram.get("minBytes", "0"), minimum=0)
                high = _int64(ram["maxBytes"]) if "maxBytes" in ram else None
                if high is not None and low >= high:
                    raise PlayError("RAM minimum must be below the exclusive maximum.")
            for key in ("includedDeviceIds", "excludedDeviceIds"):
                for device in selector.get(key, []):
                    _text(device.get("buildBrand"))
                    _text(device.get("buildDevice"))
            for key in ("requiredSystemFeatures", "forbiddenSystemFeatures"):
                for feature in selector.get(key, []):
                    _text(feature.get("name"))
            for chip in selector.get("systemOnChips", []):
                _text(chip.get("manufacturer"))
                _text(chip.get("model"))
    if len(set(names)) != len(names):
        raise PlayError("Device group names must be unique within this configuration.")
    if "deviceTierSet" in body:
        tiers = body["deviceTierSet"].get("deviceTiers")
        if not tiers:
            raise PlayError("Device tier sets require explicit tiers.")
        levels = []
        for tier in tiers:
            level = tier.get("level")
            if type(level) is not int or level < 1:
                raise PlayError("Explicit tiers require positive levels; level zero is implicit.")
            levels.append(level)
            references = tier.get("deviceGroupNames")
            _distinct(references)
            if not set(references) <= set(names):
                raise PlayError("Device tiers may only reference groups in this configuration.")
        if sorted(levels) != list(range(1, len(levels) + 1)):
            raise PlayError("Device tier levels must be unique and contiguous starting at one.")
    countries = []
    for country_set in body.get("userCountrySets", []):
        _text(country_set.get("name"))
        countries.append(country_set["name"])
        _countries(country_set.get("countryCodes"))
    if len(set(countries)) != len(countries):
        raise PlayError("Country set names must be unique within this configuration.")


def _variant(body):
    device = body.get("deviceSpec", {})
    if set(device) != {"screenDensity", "supportedAbis", "supportedLocales"}:
        raise PlayError("System APK creation requires the complete explicit device specification.")
    if type(device["screenDensity"]) is not int or device["screenDensity"] <= 0:
        raise PlayError("Screen density must be a positive uint32 integer.")
    for field in ("supportedAbis", "supportedLocales"):
        _distinct(device[field])
        for value in device[field]:
            _text(value, limit=100)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise PlayError("Device ABIs and locales must be explicit bounded identifiers.")


def request_spec(method_id, parameters, body=None):
    if not isinstance(method_id, str) or method_id not in SUPPORT_METHODS:
        raise PlayError("Method is outside the implemented support write capability.")
    validate_request(method_id, parameters, body)
    if not isinstance(body, dict):
        raise PlayError("Support writes require an explicit object body, including empty actions.")
    if len(parameters["packageName"]) > 255 or not PACKAGE.fullmatch(parameters["packageName"]):
        raise PlayError("Invalid Android package name.")
    for field in ("appRecoveryId", "versionCode"):
        if field in parameters:
            _int64(parameters[field])
    if "reviewId" in parameters:
        path_segment(parameters["reviewId"], limit=1024)
    definition = methods()[method_id]
    _write_fields(body, definition["request"])
    name = method_id.removeprefix(PREFIX)
    if name == "reviews.reply":
        _text(body.get("replyText"), limit=350)
    elif name == "applications.dataSafety":
        _text(body.get("safetyLabels"), limit=64 * 1024)
    elif name == "applications.deviceTierConfigs.create":
        _device_config(body)
    elif name == "apprecovery.create":
        if body.get("remoteInAppUpdate", {}).get("isRemoteInAppUpdateRequested") is not True:
            raise PlayError("Recovery creation requires an explicit remote in-app update action.")
        validate_targeting(body.get("targeting"))
    elif name == "apprecovery.addTargeting":
        validate_targeting(body.get("targetingUpdate"), update=True)
    elif name == "systemapks.variants.create":
        _variant(body)
    path = definition["path"]
    query = {}
    for key, value in parameters.items():
        if definition["parameters"][key]["location"] == "path":
            path = path.replace("{" + key + "}", quote(value, safe=""))
        else:
            query[key] = str(value).lower() if type(value) is bool else value
    return {
        "method": method_id,
        "http_method": definition["httpMethod"],
        "url": "https://androidpublisher.googleapis.com/"
        + path
        + ("?" + urlencode(query) if query else ""),
        "parameters": deepcopy(parameters),
        "body": deepcopy(body),
        "effects": [EFFECTS[name]],
        "empty_response": method_id in EMPTY_RESPONSE_METHODS,
    }


def validate_timestamp(timestamp):
    """Validate provider review timestamps before trusting mutation or baseline data."""
    if not isinstance(timestamp, dict) or set(timestamp) - {"seconds", "nanos"}:
        raise PlayError("Review data has an invalid timestamp.")
    seconds = timestamp.get("seconds")
    if not isinstance(seconds, str) or not re.fullmatch(r"-?(0|[1-9][0-9]{0,18})", seconds):
        raise PlayError("Review data has an invalid timestamp.")
    if (
        not -(2**63) <= int(seconds) <= 2**63 - 1
        or type(timestamp.get("nanos", 0)) is not int
        or not 0 <= timestamp.get("nanos", 0) < 10**9
    ):
        raise PlayError("Review data has an invalid timestamp.")


def validate_mutation_response(spec, data):
    """Raise on inconclusive responses; callers must retain an unknown write outcome."""
    method_id = spec["method"]
    if method_id not in SUPPORT_METHODS or not isinstance(data, dict):
        raise PlayError("Unexpected support mutation response; outcome is unknown.")
    try:
        json.dumps(data, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PlayError("Unexpected support mutation response; outcome is unknown.") from None
    schema = json_schema(methods()[method_id]["response"])
    schema["$defs"] = {
        key: json_schema(value)
        for key, value in definitions()["androidpublisher"]["schemas"].items()
    }
    if not Draft202012Validator(schema).is_valid(data):
        raise PlayError(
            "Support mutation response violates the pinned contract; outcome is unknown."
        )
    name = method_id.removeprefix(PREFIX)
    if name == "reviews.reply":
        result = data.get("result", {})
        if not isinstance(result.get("replyText"), str) or not result.get("lastEdited"):
            raise PlayError("Review reply response lacks its applied text or timestamp.")
        validate_timestamp(result["lastEdited"])
    elif name == "applications.deviceTierConfigs.create":
        _int64(data.get("deviceTierConfigId"))
    elif name == "apprecovery.create":
        _int64(data.get("appRecoveryId"))
        if data.get("status") != "RECOVERY_STATUS_DRAFT":
            raise PlayError("Recovery creation did not return the documented draft status.")
        if data.get("targeting") != spec["body"]["targeting"]:
            raise PlayError("Recovery creation response does not match the requested targeting.")
    elif name == "systemapks.variants.create":
        if type(data.get("variantId")) is not int or not 0 <= data["variantId"] <= 2**32 - 1:
            raise PlayError("System APK response lacks its generated variant identity.")
        if data.get("deviceSpec") != spec["body"]["deviceSpec"]:
            raise PlayError(
                "System APK response does not match the requested device specification."
            )
