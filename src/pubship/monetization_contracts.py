"""Fixed local commerce contracts; no generic dispatch or implicit create-by-patch."""

import re
from urllib.parse import quote, urlencode

from .config import PACKAGE
from .contracts import definitions, methods, validate_request
from .coverage import (
    LEGACY_PRODUCT_WRITE_METHODS,
    MONETIZATION_QUERY_METHODS,
    MONETIZATION_WRITE_METHODS,
    ONETIME_WRITE_METHODS,
)
from .errors import PlayError

PREFIX = "androidpublisher.monetization."
SUB = PREFIX + "subscriptions."
OFFER = SUB + "basePlans.offers."
IDENTITIES = ("packageName", "productId", "basePlanId", "purchaseOptionId", "offerId")
SUB_MASKS = {"basePlans", "listings", "taxAndComplianceSettings", "restrictedPaymentCountries"}
OFFER_MASKS = {"phases", "regionalConfigs", "offerTags", "otherRegionsConfig", "targeting"}


def identifier(value, field, *, wildcard=False):
    if wildcard and value == "-":
        return value
    pattern = {
        "packageName": PACKAGE.pattern,
        "productId": r"[a-z0-9][a-z0-9_.]{0,39}",
        "basePlanId": r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
        "purchaseOptionId": r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
        "offerId": r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    }[field]
    if not isinstance(value, str) or len(value) > 255 or not re.fullmatch(pattern, value):
        raise PlayError("Commerce identities must be explicit valid, unencoded identifiers.")
    return value


def _identity(resource, parent, fields):
    if not isinstance(resource, dict):
        raise PlayError("A commerce resource must contain its explicit identities.")
    for field in fields:
        identifier(resource.get(field), field)
        if field in parent and parent[field] != "-" and resource[field] != parent[field]:
            raise PlayError("Nested commerce identity is outside the declared parent.")
    return {field: resource[field] for field in fields}


def _output_fields(value, node):
    if "$ref" in node:
        node = definitions()["androidpublisher"]["schemas"][node["$ref"]]
    if isinstance(value, dict):
        for key, item in value.items():
            child = node.get("properties", {}).get(key, {})
            if child.get("readOnly"):
                raise PlayError("Output-only commerce fields cannot be sent as write intent.")
            _output_fields(item, child)
    elif isinstance(value, list):
        for item in value:
            _output_fields(item, node.get("items", {}))


def _version(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(ord(c) < 32 or ord(c) > 126 for c in value)
    ):
        raise PlayError("Supply an explicit regionsVersion.version; none is chosen automatically.")


def _resource(resource, parent, *, offer, create, options):
    fields = ("packageName", "productId", "basePlanId", "offerId") if offer else IDENTITIES[:2]
    target = _identity(resource, parent, fields)
    _output_fields(resource, {"$ref": "SubscriptionOffer" if offer else "Subscription"})
    if options.get("allowMissing", False) is not False:
        raise PlayError(
            "allowMissing=true is not supported; use an explicitly enabled create method."
        )
    _version(
        options.get("regionsVersion.version", options.get("regionsVersion", {}).get("version"))
    )
    if not create:
        mask = options.get("updateMask")
        allowed = OFFER_MASKS if offer else SUB_MASKS
        parts = mask.split(",") if isinstance(mask, str) else []
        if not parts or len(set(parts)) != len(parts) or any(p not in allowed for p in parts):
            raise PlayError(
                "Use distinct supported top-level updateMask fields; no wildcard or identity masks."
            )
        if any(p not in resource for p in parts):
            raise PlayError("Every masked field must be explicitly present, including clears.")
        if set(resource) - set(fields) - set(parts):
            raise PlayError(
                "Body contains unmasked write fields; make the intended field mask explicit."
            )
    if offer:
        if (create or "phases" in resource) and not 1 <= len(resource.get("phases", [])) <= 2:
            raise PlayError("An offer requires one or two ordered phases.")
        if (create or "regionalConfigs" in resource) and not resource.get("regionalConfigs"):
            raise PlayError("An offer requires at least one explicit regional configuration.")
        if len(resource.get("offerTags", [])) > 20:
            raise PlayError("An offer supports at most 20 tags.")
    else:
        if (create or "listings" in resource) and not resource.get("listings"):
            raise PlayError("Subscription listings must include the app's default language.")
        plans = resource.get("basePlans", [])
        seen = set()
        for plan in plans:
            plan_id = identifier(plan.get("basePlanId"), "basePlanId")
            if plan_id in seen:
                raise PlayError("Base plan identities must be distinct.")
            seen.add(plan_id)
            if (
                sum(
                    k in plan
                    for k in (
                        "autoRenewingBasePlanType",
                        "prepaidBasePlanType",
                        "installmentsBasePlanType",
                    )
                )
                != 1
            ):
                raise PlayError("Each base plan must declare exactly one plan type.")
    return target


def _requests(body):
    requests = body.get("requests") if isinstance(body, dict) else None
    if not isinstance(requests, list) or not 1 <= len(requests) <= 100:
        raise PlayError("Commerce batches require 1 through 100 distinct targets.")
    return requests


def request_spec(method_id, parameters, body=None, *, query=False):
    if not query and isinstance(method_id, str) and method_id in LEGACY_PRODUCT_WRITE_METHODS:
        from .legacy_product_contracts import request_spec as legacy_request

        return legacy_request(method_id, parameters, body)
    if not query and isinstance(method_id, str) and method_id in ONETIME_WRITE_METHODS:
        from .onetime_contracts import request_spec as one_time_request

        return one_time_request(method_id, parameters, body)
    allowed = MONETIZATION_QUERY_METHODS if query else MONETIZATION_WRITE_METHODS
    if not isinstance(method_id, str) or method_id not in allowed:
        raise PlayError("Method is outside this implemented commerce capability.")
    validate_request(method_id, parameters, body)
    method = methods()[method_id]
    batch = method_id.endswith((".batchGet", ".batchUpdate", ".batchUpdateStates"))
    validate_id = identifier
    if method_id.startswith(PREFIX + "onetimeproducts."):
        from .onetime_contracts import _id as validate_id
    for field in IDENTITIES:
        if field in parameters:
            validate_id(
                parameters[field], field, wildcard=batch and field not in {"packageName", "offerId"}
            )
    if parameters.get("productId") == "-" and any(
        parameters.get(f, "-") != "-" for f in ("basePlanId", "purchaseOptionId")
    ):
        raise PlayError("An all-products batch requires the matching all-parent selector.")
    targets = []
    if method_id == PREFIX + "convertRegionPrices":
        price = body.get("price", {})
        units, nanos = price.get("units", "0"), price.get("nanos", 0)
        if (
            not re.fullmatch(r"[A-Z]{3}", price.get("currencyCode", ""))
            or not re.fullmatch(r"0|[1-9][0-9]{0,18}", units)
            or int(units) > 2**63 - 1
            or type(nanos) is not int
            or not 0 <= nanos <= 999999999
        ):
            raise PlayError(
                "Price conversion requires a nonnegative Money value and ISO currency code."
            )
    elif batch:
        offer = ".offers." in method_id
        one_time = ".onetimeproducts." in method_id
        for item in _requests(body):
            if method_id.endswith(".batchUpdate"):
                field = "subscriptionOffer" if offer else "subscription"
                target = _resource(
                    item.get(field), parameters, offer=offer, create=False, options=item
                )
            else:
                if method_id.endswith(".batchUpdateStates"):
                    keys = (
                        {"activateSubscriptionOfferRequest", "deactivateSubscriptionOfferRequest"}
                        if offer
                        else {"activateBasePlanRequest", "deactivateBasePlanRequest"}
                    )
                    if len(item) != 1 or not set(item).issubset(keys):
                        raise PlayError(
                            "A state update must choose exactly one activate or deactivate action."
                        )
                    item = next(iter(item.values()))
                fields = [
                    "packageName",
                    "productId",
                    "purchaseOptionId" if one_time else "basePlanId",
                ]
                if offer:
                    fields.append("offerId")
                validate_identity = _identity
                if one_time:
                    from .onetime_contracts import _identity as validate_identity
                target = validate_identity(item, parameters, fields)
            targets.append(target)
        if len({tuple(sorted(t.items())) for t in targets}) != len(targets):
            raise PlayError("Commerce batch targets must be distinct.")
    elif method_id.endswith((".create", ".patch")):
        targets.append(
            _resource(
                body,
                parameters,
                offer=method_id.startswith(OFFER),
                create=method_id.endswith(".create"),
                options=parameters,
            )
        )
    else:
        targets.append({k: v for k, v in parameters.items() if k in IDENTITIES})
        if body is not None and set(body) - {"latencyTolerance"}:
            raise PlayError(
                "Single state actions accept only optional latencyTolerance in the body."
            )
    path = method["path"]
    params = {}
    for key, value in parameters.items():
        if method["parameters"][key]["location"] == "path":
            path = path.replace("{" + key + "}", quote(value, safe=""))
        else:
            params[key] = str(value).lower() if type(value) is bool else value
    if "{" in path or "}" in path:
        raise PlayError("Incomplete fixed commerce path.")
    return {
        "method": method_id,
        "http_method": method["httpMethod"],
        "url": "https://androidpublisher.googleapis.com/"
        + path
        + ("?" + urlencode(params) if params else ""),
        "body": body,
        "targets": targets,
    }


def observations(spec):
    """Full parent/offer snapshots; creates retain explicit 404 observations, not absence proof."""
    if spec["method"] in LEGACY_PRODUCT_WRITE_METHODS:
        from .legacy_product_contracts import observations as legacy_observations

        return legacy_observations(spec)
    if spec["method"] in ONETIME_WRITE_METHODS:
        from .onetime_contracts import observations as one_time_observations

        return one_time_observations(spec)
    result = {}
    for target in spec["targets"]:
        root = (
            "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
            + target["packageName"]
            + "/subscriptions/"
            + target["productId"]
        )
        parent = {k: target[k] for k in ("packageName", "productId")}
        create_sub = spec["method"] == SUB + "create"
        result[root] = {"identity": parent, "may_be_unobserved": create_sub, "create": create_sub}
        if "offerId" in target:
            url = root + "/basePlans/" + target["basePlanId"] + "/offers/" + target["offerId"]
            create_offer = spec["method"] == OFFER + "create"
            result[url] = {
                "identity": target,
                "may_be_unobserved": create_offer,
                "create": create_offer,
            }
    return result


def validate_observation(observation, expected, *, preflight=True):
    if expected.get("legacy_product"):
        from .legacy_product_contracts import validate_observation as validate_legacy

        return validate_legacy(observation, expected, preflight)
    if expected.get("one_time"):
        from .onetime_contracts import validate_observation as validate_one_time

        return validate_one_time(observation, expected, preflight)
    if not isinstance(observation, dict) or observation.get("http_status") not in {200, 404}:
        raise PlayError("Commerce baseline is not an identifiable resource observation.")
    if observation["http_status"] == 404:
        if preflight and not expected["may_be_unobserved"]:
            raise PlayError(
                "An existing commerce resource could not be observed; no write attempted."
            )
        return
    data = observation.get("data")
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in expected["identity"].items()):
        raise PlayError("Commerce response identity does not match the requested resource.")
    if preflight and expected["create"]:
        raise PlayError("Create target was already observed; choose an explicit update instead.")


def require_base_plans(spec, snapshots):
    if spec["method"] in ONETIME_WRITE_METHODS:
        from .onetime_contracts import require_purchase_options

        return require_purchase_options(spec, snapshots)
    for target in spec["targets"]:
        if "basePlanId" not in target:
            continue
        parent = next(
            (
                o["data"]
                for o in snapshots.values()
                if o["http_status"] == 200
                and o["data"].get("productId") == target["productId"]
                and "offerId" not in o["data"]
            ),
            None,
        )
        plans = parent.get("basePlans") if parent else None
        if not isinstance(plans, list) or not any(
            isinstance(p, dict) and p.get("basePlanId") == target["basePlanId"] for p in plans
        ):
            raise PlayError("Target base plan was not positively observed; no write attempted.")


def validate_mutation_response(spec, data):
    """A completed HTTP request alone cannot establish success for all batch targets."""
    if spec["method"] in LEGACY_PRODUCT_WRITE_METHODS:
        from .legacy_product_contracts import validate_mutation_response as validate_legacy

        return validate_legacy(spec, data)
    if spec["method"] in ONETIME_WRITE_METHODS:
        from .onetime_contracts import validate_mutation_response as validate_one_time

        return validate_one_time(spec, data)
    if not isinstance(data, dict):
        raise PlayError("Unexpected commerce mutation response.")
    if spec["http_method"] == "DELETE":
        if data != {}:
            raise PlayError("Unexpected deletion response.")
        return
    offer = ".offers." in spec["method"]
    batch = spec["method"].endswith((".batchUpdate", ".batchUpdateStates"))
    rows = data.get("subscriptionOffers" if offer else "subscriptions") if batch else [data]
    if not isinstance(rows, list) or len(rows) != len(spec["targets"]):
        raise PlayError("Commerce response does not account for every requested target.")
    identities = (
        ("packageName", "productId", "basePlanId", "offerId")
        if offer
        else ("packageName", "productId")
    )
    # Multiset comparison does not invent an ordering guarantee for all batch APIs.
    expected = sorted(tuple(t[k] for k in identities) for t in spec["targets"])
    actual = []
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in identities):
            raise PlayError("Commerce mutation response lacks resource identities.")
        actual.append(tuple(row[k] for k in identities))
    if sorted(actual) != expected:
        raise PlayError("Commerce mutation response targets do not match the request.")
