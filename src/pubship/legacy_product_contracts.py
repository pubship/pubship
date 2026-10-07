"""Legacy managed-product compatibility; never legacy subscription mutations."""

import re
from urllib.parse import quote, urlencode

from .contracts import methods, validate_request
from .coverage import LEGACY_PRODUCT_WRITE_METHODS
from .errors import PlayError
from .monetization_contracts import _requests, identifier

PREFIX = "androidpublisher.inappproducts."
SUBSCRIPTION_FIELDS = {
    "subscriptionPeriod",
    "trialPeriod",
    "gracePeriod",
    "subscriptionTaxesAndComplianceSettings",
}


def _sku(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.]{0,199}", value):
        raise PlayError(
            "Legacy SKU must be an explicit, unencoded product ID within 200 characters."
        )
    return value


def _identity(resource, parent):
    if not isinstance(resource, dict):
        raise PlayError("Legacy product requests require explicit app/SKU identities.")
    identifier(resource.get("packageName"), "packageName")
    _sku(resource.get("sku"))
    if resource["packageName"] != parent["packageName"] or (
        "sku" in parent and resource["sku"] != parent["sku"]
    ):
        raise PlayError("Legacy product identity is outside the declared app/SKU.")
    return {k: resource[k] for k in ("packageName", "sku")}


def _price(price):
    if (
        not isinstance(price, dict)
        or not isinstance(price.get("currency"), str)
        or not re.fullmatch(r"[A-Z]{3}", price["currency"])
        or not isinstance(price.get("priceMicros"), str)
        or not re.fullmatch(r"[1-9][0-9]{0,18}", price["priceMicros"])
        or int(price["priceMicros"]) > 2**63 - 1
    ):
        raise PlayError(
            "Legacy product prices require a currency and positive bounded decimal priceMicros."
        )


def _resource(body, parent, complete):
    target = _identity(body, parent)
    if SUBSCRIPTION_FIELDS & set(body) or body.get("purchaseType", "managedUser") != "managedUser":
        raise PlayError(
            "Legacy writes support managed products only; use modern subscription methods."
        )
    if complete and (
        body.get("purchaseType") != "managedUser"
        or body.get("status") not in {"active", "inactive"}
        or not body.get("defaultLanguage")
        or body["defaultLanguage"] not in body.get("listings", {})
        or "defaultPrice" not in body
    ):
        raise PlayError(
            "Insert/full update requires explicit managed type, active/inactive state, default language/listing and default price."
        )
    if "status" in body and body["status"] not in {"active", "inactive"}:
        raise PlayError("Choose active or inactive explicitly.")
    if "defaultPrice" in body:
        _price(body["defaultPrice"])
    for price in body.get("prices", {}).values():
        _price(price)
    return target


def request_spec(method_id, parameters, body=None):
    if method_id not in LEGACY_PRODUCT_WRITE_METHODS:
        raise PlayError("Unsupported legacy product write method.")
    validate_request(method_id, parameters, body)
    identifier(parameters["packageName"], "packageName")
    if "sku" in parameters:
        _sku(parameters["sku"])
    action = method_id.removeprefix(PREFIX)
    targets, modes = [], []
    autoconvert = False
    additional_grants = []
    items = _requests(body) if action.startswith("batch") else [None]
    for item in items:
        options = item if item is not None else parameters
        upsert = options.get("allowMissing", False)
        if type(upsert) is not bool:
            raise PlayError("allowMissing must be a JSON boolean.")
        if upsert:
            additional_grants = [PREFIX + "insert"]
        autoconvert |= options.get("autoConvertMissingPrices", False) is True
        if action in {"delete", "batchDelete"}:
            target = _identity(item if item is not None else parameters, parameters)
        else:
            if item is not None:
                parent = _identity(item, parameters)
                resource = item.get("inappproduct")
            else:
                parent = parameters
                resource = body
            target = _resource(resource, parent, complete=action != "patch")
        targets.append(target)
        modes.append("create" if action == "insert" else "upsert" if upsert else "existing")
    if len({tuple(t.items()) for t in targets}) != len(targets):
        raise PlayError("Legacy batch targets must be distinct.")
    method = methods()[method_id]
    path = method["path"]
    query = {}
    for key, value in parameters.items():
        if method["parameters"][key]["location"] == "path":
            path = path.replace("{" + key + "}", quote(value, safe=""))
        else:
            query[key] = str(value).lower() if type(value) is bool else value
    effects = [
        "Direct live legacy managed-product mutation; no edit commit. Only eligible, unmigrated managed products are supported; use modern one-time products for migrated catalogs."
    ]
    if action in {"update", "batchUpdate"}:
        effects.append(
            "This sends complete caller-declared PUT/update resources without merging previous fields."
        )
    if any(mode == "upsert" for mode in modes):
        effects.append(
            "Explicit allowMissing may create resources and requires the additional insert method grant."
        )
    if autoconvert:
        effects.append(
            "autoConvertMissingPrices=true asks Google to generate prices for targeted regions lacking explicit prices."
        )
    if action in {"delete", "batchDelete"}:
        effects.append("Deletes the selected managed products subject to provider eligibility.")
    else:
        effects.append("Product status and prices can affect live purchase availability.")
    return {
        "method": method_id,
        "http_method": method["httpMethod"],
        "url": "https://androidpublisher.googleapis.com/"
        + path
        + ("?" + urlencode(query) if query else ""),
        "body": body,
        "targets": targets,
        "modes": modes,
        "empty_response": "response" not in method,
        "additional_method_grants": additional_grants,
        "effects": " ".join(effects),
    }


def observations(spec):
    result = {}
    for target, mode in zip(spec["targets"], spec["modes"], strict=True):
        path = methods()[PREFIX + "get"]["path"]
        for key, value in target.items():
            path = path.replace("{" + key + "}", quote(value, safe=""))
        result["https://androidpublisher.googleapis.com/" + path] = {
            "legacy_product": True,
            "identity": target,
            "mode": mode,
        }
    return result


def validate_observation(observed, expected, preflight=True):
    if not isinstance(observed, dict) or observed.get("http_status") not in {200, 404}:
        raise PlayError("Legacy baseline is not a usable resource observation.")
    if observed["http_status"] == 404:
        if preflight and expected["mode"] == "existing":
            raise PlayError("Legacy target unavailable; no write attempted.")
        return
    data = observed.get("data")
    if (
        not isinstance(data, dict)
        or any(data.get(k) != v for k, v in expected["identity"].items())
        or data.get("purchaseType") != "managedUser"
    ):
        raise PlayError("Legacy resource identity/type is not the requested managed product.")
    if preflight and expected["mode"] == "create":
        raise PlayError("Insert target already observed; choose an explicit update.")


def validate_mutation_response(spec, data):
    if spec["empty_response"]:
        if data != {}:
            raise PlayError("Unexpected legacy deletion response.")
        return
    rows = (
        data.get("inappproducts")
        if spec["method"].endswith(".batchUpdate") and isinstance(data, dict)
        else [data]
    )
    if not isinstance(rows, list) or len(rows) != len(spec["targets"]):
        raise PlayError("Legacy response does not account for every target.")
    actual = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("purchaseType") != "managedUser"
            or not isinstance(row.get("packageName"), str)
            or not isinstance(row.get("sku"), str)
        ):
            raise PlayError("Legacy response lacks matching managed-product identities.")
        actual.append((row["packageName"], row["sku"]))
    if sorted(actual) != sorted((t["packageName"], t["sku"]) for t in spec["targets"]):
        raise PlayError("Legacy response targets differ from the request.")
