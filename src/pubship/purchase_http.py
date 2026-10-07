"""One-attempt purchase writes and private, qualified observations on fixed routes."""

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import UTC, datetime
from functools import lru_cache
from http.client import HTTPException
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .auth import validate_token
from .contracts import definitions, json_schema
from .errors import PlayError
from .http import NoRedirect, finite_float
from .provider_transport import bounded_opener, dispatch_check
from .publisher_reads_contracts import validate_publisher_read
from .purchase_contracts import (
    package_for_spec,
    request_spec,
    validate_mutation_response,
    validate_price,
    validate_timestamp,
)

PREFIX = "androidpublisher."
PRODUCT = PREFIX + "purchases.products."
LEGACY_SUB = PREFIX + "purchases.subscriptions."
SUB_V2 = PREFIX + "purchases.subscriptionsv2."
ORDERS = PREFIX + "orders."
EXTERNAL = PREFIX + "externaltransactions."
EXTERNAL_CREATE = EXTERNAL + "createexternaltransaction"
MIGRATION = PREFIX + "monetization.subscriptions.basePlans."
MAX_BYTES = 2 * 1024 * 1024
UNKNOWN_OUTCOME = (
    "Purchase operation outcome unknown; reconcile the original targets and reporting IDs "
    "before preparing again. Do not automatically retry. This operation ID is consumed."
)
VALIDATION_UNKNOWN_OUTCOME = (
    "Purchase validation outcome unknown; no purchase mutation was requested. "
    "This operation ID is consumed; review before preparing another validation."
)
READ_ERROR = (
    "Purchase observation unavailable or malformed; no mutation was attempted by this read."
)
ACTIVE = "SUBSCRIPTION_STATE_ACTIVE"
RENEWABLE_STATES = {
    ACTIVE,
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
    "SUBSCRIPTION_STATE_ON_HOLD",
    "SUBSCRIPTION_STATE_PAUSED",
}


def _bounded(value, *, max_bytes=None):
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False).encode()
        if len(encoded) > (MAX_BYTES if max_bytes is None else max_bytes):
            raise ValueError()
        return encoded
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise PlayError(
            READ_ERROR
            if max_bytes is None
            else "Purchase observations exceed the hosted preparation limit or contain invalid JSON."
        ) from None


def _send(
    url,
    token,
    *,
    method="GET",
    body=None,
    empty_success=False,
    allow_unavailable=False,
    validate_only=False,
    deadline=None,
    _execution_authorization=None,
):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "androidpublisher.googleapis.com"
        or parsed.fragment
        or method not in {"GET", "POST"}
    ):
        raise PlayError("Purchase requests require a fixed Google Publisher route.")
    mutation = method == "POST"
    outcome = VALIDATION_UNKNOWN_OUTCOME if validate_only else UNKNOWN_OUTCOME
    request = urllib.request.Request(
        url,
        method=method,
        data=_bounded(body) if body is not None else None,
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
            raise ValueError()
        if status == 204:
            if content or not empty_success:
                raise ValueError()
            return {}
        if empty_success and not content.strip():
            return {}
        result = json.loads(content, parse_float=finite_float, parse_constant=finite_float)
        if not isinstance(result, dict):
            raise ValueError()
        _bounded(result)
        return result
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if not mutation and allow_unavailable and status == 404:
            return None
        raise PlayError(
            f"Google API HTTP {status}. " + (outcome if mutation else READ_ERROR)
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
        raise PlayError(outcome if mutation else READ_ERROR) from None


def execute(method, parameters, body, token, *, deadline=None, _execution_authorization=None):
    spec = request_spec(method, parameters, body)
    if spec["http_method"] != "POST":
        raise PlayError("Only the fixed purchase POST methods are implemented.")
    data = _send(
        spec["url"],
        token,
        method="POST",
        body=spec["body"],
        empty_success=spec["empty_response"],
        validate_only=spec.get("validate_only", False),
        deadline=deadline,
        _execution_authorization=_execution_authorization,
    )
    try:
        validate_mutation_response(spec, data)
    except Exception:
        raise PlayError(
            VALIDATION_UNKNOWN_OUTCOME if spec.get("validate_only") else UNKNOWN_OUTCOME
        ) from None
    return data


def validate_observed_response(spec, observation, mutation):
    """Correlate returned item identities with the privately reviewed purchase."""
    if spec["method"] == SUB_V2 + "defer":
        expected = {item["productId"] for item in observation["lineItems"]}
        actual = {item["productId"] for item in mutation["itemExpiryTimeDetails"]}
        if actual != expected:
            raise PlayError("Deferral response does not cover the reviewed subscription items.")
    elif spec["method"] == EXTERNAL + "refundexternaltransaction":
        for key in ("originalPreTaxAmount", "originalTaxAmount"):
            if validate_price(mutation[key]) != validate_price(observation[key]):
                raise PlayError("Refund response changed the originally reported economic amounts.")
        if "partialRefund" in spec["body"]:
            currency, before = validate_price(observation["currentPreTaxAmount"])
            _, requested = validate_price(spec["body"]["partialRefund"]["refundPreTaxAmount"])
            if validate_price(mutation["currentPreTaxAmount"]) != (currency, before - requested):
                raise PlayError(
                    "Refund response does not confirm the reviewed remaining pre-tax amount."
                )


@lru_cache(maxsize=8)
def _validator(name):
    schemas = definitions()["androidpublisher"]["schemas"]
    return Draft202012Validator(
        {
            "$ref": "#/$defs/" + name,
            "$defs": {key: json_schema(value) for key, value in schemas.items()},
        }
    )


def _resource(name, data):
    _bounded(data)
    if not isinstance(data, dict) or not _validator(name).is_valid(data):
        raise PlayError(READ_ERROR)


def _get(
    method,
    parameters,
    token,
    *,
    sensitive=True,
    allow_unavailable=False,
    deadline=None,
    _execution_authorization=None,
    _max_snapshot_bytes=None,
):
    _, url = validate_publisher_read(method, parameters, sensitive=sensitive)
    if _execution_authorization is not None:
        _execution_authorization()
    _fresh(deadline)
    data = _send(url, token, allow_unavailable=allow_unavailable)
    if _max_snapshot_bytes is not None:
        _bounded(data, max_bytes=_max_snapshot_bytes)
    return data


def _timestamp_ns(value):
    validate_timestamp(value)
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value
    )
    if not match:
        raise PlayError(READ_ERROR)
    base, fractional, zone = match.groups()
    moment = datetime.fromisoformat(base + zone.replace("Z", "+00:00"))
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 10**9 + int((fractional or "").ljust(9, "0"))


def _money(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("currencyCode"), str)
        or not re.fullmatch(r"[A-Z]{3}", value["currencyCode"])
        or not isinstance(value.get("units", "0"), str)
        or not re.fullmatch(r"0|[1-9][0-9]{0,18}", value.get("units", "0"))
        or int(value.get("units", "0")) > 2**63 - 1
        or type(value.get("nanos", 0)) is not int
        or not 0 <= value.get("nanos", 0) < 10**9
    ):
        raise PlayError(READ_ERROR)
    return {
        "currencyCode": value["currencyCode"],
        "units": value.get("units", "0"),
        "nanos": value.get("nanos", 0),
    }


def _product(spec, data):
    _resource("ProductPurchase", data)
    p = spec["parameters"]
    if any(
        key in data and data[key] != p[param]
        for key, param in (("productId", "productId"), ("purchaseToken", "token"))
    ):
        raise PlayError("Purchase observation returned a different target identity.")
    for key, allowed in (
        ("purchaseState", {0, 1, 2}),
        ("acknowledgementState", {0, 1}),
        ("consumptionState", {0, 1}),
    ):
        if type(data.get(key)) is not int or data[key] not in allowed:
            raise PlayError(READ_ERROR)


def _subscription(data):
    _resource("SubscriptionPurchaseV2", data)
    items = data.get("lineItems")
    if (
        not data.get("subscriptionState")
        or data["subscriptionState"] == "SUBSCRIPTION_STATE_UNSPECIFIED"
        or data.get("acknowledgementState")
        not in {"ACKNOWLEDGEMENT_STATE_PENDING", "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"}
        or not isinstance(items, list)
        or not 1 <= len(items) <= 100
    ):
        raise PlayError(READ_ERROR)
    for item in items:
        if not item.get("productId") or len(set(item) & {"autoRenewingPlan", "prepaidPlan"}) != 1:
            raise PlayError(READ_ERROR)
        _timestamp_ns(item.get("expiryTime"))
    products = [item["productId"] for item in items]
    if len(set(products)) != len(products):
        raise PlayError("Subscription observation contains ambiguous duplicate items.")


def _order(spec, data):
    _resource("Order", data)
    if data.get("orderId") != spec["parameters"]["orderId"] or data.get("state") not in {
        "PENDING",
        "PROCESSED",
        "CANCELED",
        "PENDING_REFUND",
        "PARTIALLY_REFUNDED",
        "REFUNDED",
    }:
        raise PlayError(READ_ERROR)
    _money(data.get("total"))
    items = data.get("lineItems")
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise PlayError(READ_ERROR)
    for item in items:
        if not item.get("productId"):
            raise PlayError(READ_ERROR)
        _money(item.get("total"))


def _external_name(spec):
    p = spec["parameters"]
    return (
        p["parent"] + "/externalTransactions/" + p["externalTransactionId"]
        if spec["method"] == EXTERNAL_CREATE
        else p["name"]
    )


def _external(spec, data):
    _resource("ExternalTransaction", data)
    if (
        data.get("packageName") != package_for_spec(spec)
        or data.get("externalTransactionId") != _external_name(spec).split("/")[-1]
        or data.get("transactionState") not in {"TRANSACTION_REPORTED", "TRANSACTION_CANCELED"}
    ):
        raise PlayError(READ_ERROR)
    values = [
        validate_price(data.get(key))
        for key in (
            "originalPreTaxAmount",
            "originalTaxAmount",
            "currentPreTaxAmount",
            "currentTaxAmount",
        )
    ]
    if (
        len({currency for currency, _ in values}) != 1
        or values[2][1] > values[0][1]
        or values[3][1] > values[1][1]
    ):
        raise PlayError(READ_ERROR)


def _migrations(spec):
    return (
        spec["body"]["requests"]
        if spec["method"].endswith("batchMigratePrices")
        else [spec["body"]]
    )


def _fresh(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise PlayError("Purchase preparation expired; no further observation request attempted.")


def _catalog(
    spec, token, deadline=None, *, _execution_authorization=None, _max_snapshot_bytes=None
):
    observed, targets = {}, []
    for target in _migrations(spec):
        product = target["productId"]
        if product not in observed:
            _fresh(deadline)
            data = _get(
                PREFIX + "monetization.subscriptions.get",
                {"packageName": target["packageName"], "productId": product},
                token,
                sensitive=False,
                deadline=deadline,
                _execution_authorization=_execution_authorization,
                _max_snapshot_bytes=_max_snapshot_bytes,
            )
            _resource("Subscription", data)
            if data.get("packageName") != target["packageName"] or data.get("productId") != product:
                raise PlayError(READ_ERROR)
            observed[product] = data
            if _max_snapshot_bytes is not None:
                _bounded([observed, targets], max_bytes=_max_snapshot_bytes)
        plans = observed[product].get("basePlans")
        if not isinstance(plans, list) or len(plans) > 100:
            raise PlayError(READ_ERROR)
        matches = [plan for plan in plans if plan.get("basePlanId") == target["basePlanId"]]
        if len(matches) != 1:
            raise PlayError("Price migration observation requires exactly one selected base plan.")
        plan = matches[0]
        regions = plan.get("regionalConfigs")
        if not isinstance(regions, list) or not 1 <= len(regions) <= 1000:
            raise PlayError(READ_ERROR)
        codes = [region.get("regionCode") for region in regions]
        if any(
            not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code) for code in codes
        ) or len(set(codes)) != len(codes):
            raise PlayError(READ_ERROR)
        for requested in target["regionalPriceMigrations"]:
            matches = [
                region for region in regions if region["regionCode"] == requested["regionCode"]
            ]
            if len(matches) != 1:
                raise PlayError("Requested migration region lacks an observed destination price.")
            _money(matches[0].get("price"))
        targets.append(
            {
                "packageName": target["packageName"],
                "productId": product,
                "basePlanId": target["basePlanId"],
                "basePlan": deepcopy(plan),
            }
        )
        if _max_snapshot_bytes is not None:
            _bounded([observed, targets], max_bytes=_max_snapshot_bytes)
    result = {"targets": targets}
    _bounded(result)
    return result


def observe(
    spec,
    token,
    mutation_response=None,
    *,
    deadline=None,
    _execution_authorization=None,
    _max_snapshot_bytes=None,
):
    _fresh(deadline)
    read_options = {
        "deadline": deadline,
        "_execution_authorization": _execution_authorization,
        "_max_snapshot_bytes": _max_snapshot_bytes,
    }
    method, p = spec["method"], spec["parameters"]
    if method.startswith(PRODUCT):
        data = _get(PRODUCT + "get", p, token, **read_options)
        _product(spec, data)
    elif method.startswith((LEGACY_SUB, SUB_V2)):
        data = _get(
            SUB_V2 + "get",
            {"packageName": p["packageName"], "token": p["token"]},
            token,
            **read_options,
        )
        _subscription(data)
    elif method.startswith(ORDERS):
        if method == ORDERS + "reviewrefund" and mutation_response is not None:
            return None
        data = _get(
            ORDERS + "get",
            {"packageName": p["packageName"], "orderId": p["orderId"]},
            token,
            **read_options,
        )
        _order(spec, data)
    elif method.startswith(EXTERNAL):
        data = _get(
            EXTERNAL + "getexternaltransaction",
            {"name": _external_name(spec)},
            token,
            allow_unavailable=method == EXTERNAL_CREATE and mutation_response is None,
            **read_options,
        )
        if data is not None:
            _external(spec, data)
    elif method.startswith(MIGRATION):
        return _catalog(spec, token, **read_options)
    else:
        raise PlayError("No observation contract for this purchase method.")
    return data


def check_preflight(spec, observation):
    method, body = spec["method"], spec["body"]
    if method == EXTERNAL_CREATE:
        if observation is not None:
            raise PlayError(
                "The reporting identifier is already observed; reconcile it instead of creating again."
            )
        return
    if observation is None:
        raise PlayError("An explicit target observation is required before this operation.")
    if method.startswith(PRODUCT):
        state = "acknowledgementState" if method.endswith("acknowledge") else "consumptionState"
        if observation["purchaseState"] != 0 or observation[state] != 0:
            raise PlayError(
                "Product action requires a completed purchase without the requested action already applied."
            )
    elif method.startswith((LEGACY_SUB, SUB_V2)):
        items = observation["lineItems"]
        state = observation["subscriptionState"]
        if method.startswith(LEGACY_SUB) and (
            len(items) != 1 or items[0]["productId"] != spec["parameters"]["subscriptionId"]
        ):
            raise PlayError(
                "Legacy subscription actions are limited to one matching item; use v2 for add-ons."
            )
        if method.endswith("acknowledge"):
            if (
                state != ACTIVE
                or observation["acknowledgementState"] != "ACKNOWLEDGEMENT_STATE_PENDING"
            ):
                raise PlayError(
                    "Subscription acknowledgement requires an active, unacknowledged purchase."
                )
        elif method.endswith("cancel"):
            if (
                state not in RENEWABLE_STATES
                or not all("autoRenewingPlan" in item for item in items)
                or not any(
                    item["autoRenewingPlan"].get("autoRenewEnabled") is True for item in items
                )
            ):
                raise PlayError(
                    "Cancellation requires an observed renewable subscription with renewal enabled."
                )
        elif method.endswith("defer"):
            if state != ACTIVE or any(
                _timestamp_ns(item["expiryTime"]) <= time.time_ns() for item in items
            ):
                raise PlayError("Local deferral requires active, unexpired subscription items.")
            if method.startswith(LEGACY_SUB):
                actual = _timestamp_ns(items[0]["expiryTime"])
                if actual != int(body["deferralInfo"]["expectedExpiryTimeMillis"]) * 10**6:
                    raise PlayError(
                        "Expected subscription expiry differs from the observed expiry."
                    )
            elif (
                not observation.get("etag")
                or observation["etag"] != body["deferralContext"]["etag"]
            ):
                raise PlayError("Reviewed subscription ETag differs from the observed ETag.")
        elif method.endswith("revoke"):
            if state not in RENEWABLE_STATES | {"SUBSCRIPTION_STATE_CANCELED"}:
                raise PlayError("Revocation requires an observed nonterminal subscription.")
            item_refund = body["revocationContext"].get("itemBasedRefund")
            selected = [
                item
                for item in items
                if item_refund is None or item["productId"] == item_refund["productId"]
            ]
            if not selected or any(not item.get("latestSuccessfulOrderId") for item in selected):
                raise PlayError(
                    "Revocation requires an observed owned target item with a successful charge."
                )
    elif method == ORDERS + "refund":
        if observation["state"] not in {"PROCESSED", "PARTIALLY_REFUNDED"}:
            raise PlayError("Refund requires a processed or partially refunded order.")
    elif method == ORDERS + "reviewrefund":
        if observation["state"] not in {"PROCESSED", "PENDING_REFUND", "PARTIALLY_REFUNDED"}:
            raise PlayError(
                "Refund review requires a processed order with a potentially pending refund."
            )
    elif method.startswith(EXTERNAL):
        if observation["transactionState"] != "TRANSACTION_REPORTED":
            raise PlayError(
                "External refund reporting requires a reported, not fully refunded transaction."
            )
        if "partialRefund" in body:
            currency, amount = validate_price(body["partialRefund"]["refundPreTaxAmount"])
            remaining_currency, remaining = validate_price(observation["currentPreTaxAmount"])
            if currency != remaining_currency or not 0 < amount < remaining:
                raise PlayError(
                    "Partial refund must use the observed currency and be below the remaining pre-tax amount."
                )
    elif method.startswith(MIGRATION):
        for target in observation["targets"]:
            plan = target["basePlan"]
            if (
                plan.get("state") not in {"ACTIVE", "INACTIVE"}
                or len(set(plan) & {"autoRenewingBasePlanType", "installmentsBasePlanType"}) != 1
                or "prepaidBasePlanType" in plan
            ):
                raise PlayError(
                    "Price migration requires an active or inactive renewable base plan."
                )


def _select(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value}


def fingerprint(spec, observation):
    if observation is None:
        return None
    method = spec["method"]
    if method.startswith(PRODUCT):
        value = _select(
            observation,
            (
                "productId",
                "orderId",
                "purchaseState",
                "acknowledgementState",
                "consumptionState",
                "purchaseTimeMillis",
                "quantity",
                "refundableQuantity",
            ),
        )
    elif method.startswith((LEGACY_SUB, SUB_V2)):
        value = _select(
            observation,
            (
                "subscriptionState",
                "acknowledgementState",
                "etag",
                "lineItems",
                "pausedStateContext",
                "canceledStateContext",
                "onHoldStateContext",
                "inGracePeriodStateContext",
            ),
        )
    elif method.startswith(ORDERS):
        value = _select(
            observation,
            (
                "orderId",
                "purchaseToken",
                "state",
                "total",
                "tax",
                "lineItems",
                "orderHistory",
                "lastEventTime",
            ),
        )
        for item in value.get("lineItems", []):
            item.pop("productTitle", None)
    elif method.startswith(EXTERNAL):
        value = _select(
            observation,
            (
                "externalTransactionId",
                "packageName",
                "transactionState",
                "originalPreTaxAmount",
                "originalTaxAmount",
                "currentPreTaxAmount",
                "currentTaxAmount",
                "transactionTime",
            ),
        )
    else:
        value = deepcopy(observation)
        for target in value["targets"]:
            target["basePlan"].pop("offerTags", None)
            target["basePlan"]["regionalConfigs"].sort(key=lambda region: region["regionCode"])
    return hashlib.sha256(_bounded(value)).hexdigest()


def redacted_observation(spec, observation):
    """Project economic state only; never echo provider customer records or URLs."""
    if observation is None:
        return {
            "available": False,
            "absence_verified": False,
            "note": "No usable observation; this does not prove absence or the result of an action.",
        }
    method = spec["method"]
    if method.startswith(PRODUCT):
        result = _select(
            observation,
            (
                "purchaseState",
                "acknowledgementState",
                "consumptionState",
                "quantity",
                "refundableQuantity",
            ),
        )
    elif method.startswith((LEGACY_SUB, SUB_V2)):
        result = _select(observation, ("subscriptionState", "acknowledgementState"))
        result["items"] = []
        for item in observation.get("lineItems", []):
            public = _select(item, ("productId", "expiryTime"))
            public["plan_type"] = "auto_renewing" if "autoRenewingPlan" in item else "prepaid"
            if "autoRenewingPlan" in item:
                public.update(_select(item["autoRenewingPlan"], ("autoRenewEnabled",)))
            result["items"].append(public)
    elif method.startswith(ORDERS):
        result = _select(observation, ("state",))
        result["total"] = _money(observation["total"])
        result["line_item_count"] = len(observation["lineItems"])
        if method == ORDERS + "reviewrefund":
            result["refund_preference_verified"] = False
    elif method.startswith(EXTERNAL):
        result = _select(observation, ("transactionState",))
        for key in (
            "originalPreTaxAmount",
            "originalTaxAmount",
            "currentPreTaxAmount",
            "currentTaxAmount",
        ):
            if key in observation:
                currency, amount = validate_price(observation[key])
                result[key] = {"currency": currency, "priceMicros": str(amount)}
    elif method.startswith(MIGRATION):
        result = {
            "targets": [],
            "subscriber_cohort_observed": False,
            "migration_completion_verified": False,
        }
        for target, request in zip(observation["targets"], _migrations(spec), strict=True):
            selected = {entry["regionCode"] for entry in request["regionalPriceMigrations"]}
            result["targets"].append(
                {
                    **_select(target, ("productId", "basePlanId")),
                    "state": target["basePlan"]["state"],
                    "destination_prices": [
                        {"regionCode": region["regionCode"], "price": _money(region["price"])}
                        for region in target["basePlan"]["regionalConfigs"]
                        if region["regionCode"] in selected
                    ],
                }
            )
    else:
        raise PlayError("No public observation contract for this purchase method.")
    result.update(available=True, sensitive_fields_redacted=True)
    return result
