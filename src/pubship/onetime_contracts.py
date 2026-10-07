"""Modern one-time-product contracts, explicit upserts and preorder effects."""

import re
from urllib.parse import quote, urlencode

from .contracts import methods, validate_request
from .coverage import ONETIME_WRITE_METHODS
from .errors import PlayError
from .monetization_contracts import _output_fields, _requests, _version, identifier

PREFIX = "androidpublisher.monetization.onetimeproducts."
FIELDS = ("packageName", "productId", "purchaseOptionId", "offerId")
PRODUCT_MASKS = {
    "listings",
    "purchaseOptions",
    "offerTags",
    "taxAndComplianceSettings",
    "restrictedPaymentCountries",
}
OFFER_MASKS = {
    "discountedOffer",
    "preOrderOffer",
    "gameRewardOffer",
    "offerTags",
    "regionalPricingAndAvailabilityConfigs",
}


def _id(value, field, wildcard=False):
    if value == "-" and wildcard:
        return value
    if field == "packageName":
        return identifier(value, field)
    pattern = r"[a-z0-9][a-z0-9_.]{0,199}" if field == "productId" else r"[a-z0-9][a-z0-9-]{0,62}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise PlayError("One-time-product identities must be explicit valid unencoded identifiers.")
    return value


def _identity(value, parent, fields):
    if not isinstance(value, dict):
        raise PlayError("One-time-product request requires explicit resource identities.")
    for field in fields:
        _id(value.get(field), field)
        if field in parent and parent[field] != "-" and value[field] != parent[field]:
            raise PlayError("Nested one-time-product target is outside the declared app or parent.")
    return {k: value[k] for k in fields}


def _resource(resource, options, parent, offer):
    fields = FIELDS if offer else FIELDS[:2]
    target = _identity(resource, parent, fields)
    _output_fields(resource, {"$ref": "OneTimeProductOffer" if offer else "OneTimeProduct"})
    _version(
        options.get("regionsVersion.version", options.get("regionsVersion", {}).get("version"))
    )
    mask = options.get("updateMask")
    fields_to_update = mask.split(",") if isinstance(mask, str) else []
    allowed = OFFER_MASKS if offer else PRODUCT_MASKS
    if (
        not fields_to_update
        or len(set(fields_to_update)) != len(fields_to_update)
        or any(k not in allowed for k in fields_to_update)
    ):
        raise PlayError(
            "One-time updates require distinct supported top-level masks; no wildcard or identity masks."
        )
    if any(k not in resource for k in fields_to_update):
        raise PlayError("Every one-time update mask field must be explicitly present.")
    if set(resource) - set(fields) - set(fields_to_update):
        raise PlayError("One-time body contains unmasked fields; make the intent explicit.")
    upsert = options.get("allowMissing", False)
    if type(upsert) is not bool:
        raise PlayError("allowMissing must be a JSON boolean.")
    if not offer:
        if (upsert or "listings" in resource) and not resource.get("listings"):
            raise PlayError("Product upsert/listing changes require explicit localized listings.")
        if upsert and not resource.get("purchaseOptions"):
            raise PlayError(
                "Product upsert needs a complete creation-capable purchaseOptions body."
            )
        ids = set()
        for option in resource.get("purchaseOptions", []):
            value = _id(option.get("purchaseOptionId"), "purchaseOptionId")
            if value in ids or sum(k in option for k in ("buyOption", "rentOption")) != 1:
                raise PlayError("Purchase options need distinct IDs and exactly one buy/rent type.")
            ids.add(value)
    elif (
        upsert
        and sum(k in resource for k in ("discountedOffer", "preOrderOffer", "gameRewardOffer")) != 1
    ):
        raise PlayError("Offer upsert requires exactly one complete offer type.")
    if len(resource.get("offerTags", [])) > 20:
        raise PlayError("One-time products and offers support at most 20 tags.")
    return target, upsert


def request_spec(method_id, parameters, body=None):
    if method_id not in ONETIME_WRITE_METHODS:
        raise PlayError("Method is outside the one-time-product write capability.")
    validate_request(method_id, parameters, body)
    method = methods()[method_id]
    batch = ".batch" in method_id
    offer = ".offers." in method_id
    option = ".purchaseOptions." in method_id
    fields = FIELDS if offer else FIELDS[:3] if option else FIELDS[:2]
    for key in FIELDS:
        if key in parameters:
            _id(parameters[key], key, wildcard=batch and key in {"productId", "purchaseOptionId"})
    if parameters.get("productId") == "-" and parameters.get("purchaseOptionId", "-") != "-":
        raise PlayError("An all-products offer batch requires an all-purchase-options selector.")
    targets, upserts = [], []
    has_force = False
    has_cancel = method_id.endswith(".cancel")
    if batch:
        for item in _requests(body):
            upsert = False
            if method_id.endswith(".batchUpdate"):
                target, upsert = _resource(
                    item.get("oneTimeProductOffer" if offer else "oneTimeProduct"),
                    item,
                    parameters,
                    offer,
                )
            else:
                if method_id.endswith(".batchUpdateStates"):
                    union = (
                        {
                            "activateOneTimeProductOfferRequest",
                            "deactivateOneTimeProductOfferRequest",
                            "cancelOneTimeProductOfferRequest",
                        }
                        if offer
                        else {"activatePurchaseOptionRequest", "deactivatePurchaseOptionRequest"}
                    )
                    if len(item) != 1 or not set(item).issubset(union):
                        raise PlayError("State update must select exactly one supported action.")
                    has_cancel |= "cancelOneTimeProductOfferRequest" in item
                    item = next(iter(item.values()))
                target = _identity(item, parameters, fields)
                has_force |= item.get("force", False) is True
            targets.append(target)
            upserts.append(upsert)
        if len({tuple(t.items()) for t in targets}) != len(targets):
            raise PlayError("One-time batch targets must be distinct.")
        if method_id == PREFIX + "purchaseOptions.batchDelete" and len(
            {t["productId"] for t in targets}
        ) != len(targets):
            raise PlayError(
                "Purchase-option batch deletion requires different parent products per request."
            )
    elif method_id.endswith(".patch"):
        target, upsert = _resource(body, parameters, parameters, False)
        targets.append(target)
        upserts.append(upsert)
    else:
        targets.append({k: parameters[k] for k in fields})
        upserts.append(False)
        if body is not None and set(body) - {"latencyTolerance"}:
            raise PlayError(
                "Single offer state-action bodies accept only optional latencyTolerance."
            )
    path = method["path"]
    query = {}
    for k, v in parameters.items():
        if method["parameters"][k]["location"] == "path":
            path = path.replace("{" + k + "}", quote(v, safe=""))
        else:
            query[k] = str(v).lower() if type(v) is bool else v
    effects = ["Direct live one-time-product catalog mutation; no publishing-edit commit follows."]
    if any(upserts):
        effects.append(
            "Explicit allowMissing permits creation if unavailable; Google ignores the update mask for newly created resources. Existing targets are updated."
        )
    if has_force:
        effects.append(
            "force=true may delete every child offer beneath the selected purchase option. The preview does not inventory those offers."
        )
    if has_cancel:
        effects.append(
            "Preorder cancellation can cancel ALL pending orders related to the selected offer; it is not a visibility toggle."
        )
    return {
        "method": method_id,
        "http_method": method["httpMethod"],
        "url": "https://androidpublisher.googleapis.com/"
        + path
        + ("?" + urlencode(query) if query else ""),
        "body": body,
        "targets": targets,
        "upserts": upserts,
        "empty_response": "response" not in method,
        "effects": " ".join(effects),
    }


def observations(spec):
    result = {}
    get_method = methods()[PREFIX + "get"]
    for target, upsert in zip(spec["targets"], spec["upserts"], strict=True):
        url = "https://androidpublisher.googleapis.com/" + get_method["path"]
        for k in FIELDS[:2]:
            url = url.replace("{" + k + "}", quote(target[k], safe=""))
        parent = {k: target[k] for k in FIELDS[:2]}
        product_upsert = upsert and "offerId" not in target
        result[url] = {
            "identity": parent,
            "one_time": True,
            "upsert": product_upsert,
            "offer": False,
        }
        if "offerId" in target:
            # No individual one-time offer GET exists. Use its fixed one-item batch query.
            params = {k: target[k] for k in FIELDS[:3]}
            body = {"requests": [dict(target)]}
            key = (
                url
                + "/purchaseOptions/"
                + target["purchaseOptionId"]
                + "/offers/"
                + target["offerId"]
            )
            result[key] = {
                "identity": target,
                "one_time": True,
                "upsert": upsert,
                "offer": True,
                "query": {
                    "method": PREFIX + "purchaseOptions.offers.batchGet",
                    "parameters": params,
                    "body": body,
                },
            }
    return result


def validate_observation(observed, expected, preflight=True):
    status = observed.get("http_status") if isinstance(observed, dict) else None
    if status not in {200, 404}:
        raise PlayError("One-time baseline has no usable resource observation.")
    if status == 404:
        if preflight and not expected["upsert"]:
            raise PlayError("Existing one-time target could not be observed; no write attempted.")
        return
    data = observed.get("data")
    if not isinstance(data, dict):
        raise PlayError("Invalid one-time observation shape.")
    if expected["offer"]:
        rows = data.get("oneTimeProductOffers")
        # Omitted/empty collection is retained verbatim and never described as proof of absence.
        if rows is None or rows == []:
            if preflight and not expected["upsert"]:
                raise PlayError("Existing offer was not positively observed; no write attempted.")
            return
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise PlayError("One-item offer observation returned unexpected targets.")
        data = rows[0]
    if any(data.get(k) != v for k, v in expected["identity"].items()):
        raise PlayError("One-time observation identity does not match the requested target.")


def require_purchase_options(spec, snapshots):
    for target in spec["targets"]:
        if "purchaseOptionId" not in target:
            continue
        parents = [
            o["data"]
            for o in snapshots.values()
            if o["http_status"] == 200
            and isinstance(o["data"], dict)
            and o["data"].get("productId") == target["productId"]
        ]
        options = parents[0].get("purchaseOptions") if parents else None
        if not isinstance(options, list) or not any(
            isinstance(o, dict) and o.get("purchaseOptionId") == target["purchaseOptionId"]
            for o in options
        ):
            raise PlayError(
                "Parent purchase option was not positively observed; no write attempted."
            )


def validate_mutation_response(spec, data):
    if spec["empty_response"]:
        if data != {}:
            raise PlayError("Unexpected one-time deletion response.")
        return
    offer = ".offers." in spec["method"]
    batch = ".batch" in spec["method"]
    rows = (
        data.get("oneTimeProductOffers" if offer else "oneTimeProducts")
        if batch and isinstance(data, dict)
        else [data]
    )
    if not isinstance(rows, list) or len(rows) != len(spec["targets"]):
        raise PlayError("One-time response does not account for every requested target.")
    fields = FIELDS if offer else FIELDS[:2]
    for target, row in zip(spec["targets"], rows, strict=True):
        if not isinstance(row, dict) or any(row.get(k) != target[k] for k in fields):
            raise PlayError("One-time response identities/order do not match the request.")
