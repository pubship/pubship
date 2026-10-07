"""Fixed purchase-lifecycle intent, provider evidence and safe public projections.

Requests and provider responses are private: callers must use the projections
below instead of returning token-bearing routes or customer records.
"""

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from urllib.parse import quote, urlencode

from jsonschema import Draft202012Validator

from .config import PACKAGE
from .contracts import definitions, json_schema, methods, validate_request
from .errors import PlayError
from .monetization_contracts import identifier

PREFIX = "androidpublisher."
BASE = PREFIX + "monetization.subscriptions.basePlans."
EXTERNAL = PREFIX + "externaltransactions."
EFFECTS = {
    "purchases.products.acknowledge": "Acknowledges the completed product purchase. This records fulfilment acknowledgement; it does not grant app content itself.",
    "purchases.products.consume": "Consumes the product purchase so it can be purchased again. App fulfilment and consumed-content handling remain the operator's responsibility.",
    "purchases.subscriptions.acknowledge": "Acknowledges the subscription purchase using the legacy explicit single-product route; add-on subscriptions are not supported by this compatibility operation.",
    "purchases.subscriptions.cancel": "Uses the deprecated single-product cancellation API to stop subscription renewal. It does not refund the purchase. Use the v2 action for explicit cancellation semantics.",
    "purchases.subscriptions.defer": "Uses the deprecated single-product API to extend expiry, guarded by the supplied expected expiry. The existing expiry is never replaced automatically.",
    "purchases.subscriptionsv2.cancel": "Cancels according to the explicit cancellation type. USER_REQUESTED_STOP_RENEWALS permits restoration and retains installment commitments; DEVELOPER_REQUESTED_STOP_PAYMENTS prevents restoration and stops the next payment.",
    "purchases.subscriptionsv2.defer": "Defers every subscription item by the supplied duration with the supplied ETag as a concurrency check. validateOnly=true performs validation without changing the subscription.",
    "purchases.subscriptionsv2.revoke": "Revokes subscription access with exactly the requested refund mode. Full refund covers each item's latest charge; item-based refund targets the specified add-on product.",
    "orders.refund": "Refunds the order. revoke=true also terminates access and future subscription payments; consumed items require handling in the developer's app.",
    "orders.reviewrefund": "Submits the operator's refund preference and consumption evidence to Google. Submission does not establish Google's refund decision.",
    "externaltransactions.createexternaltransaction": "Reports an external payment already completed outside Play; it does not execute that payment. Retain the supplied transaction ID when reconciling uncertain outcomes.",
    "externaltransactions.refundexternaltransaction": "Reports an external refund already completed outside Play; it does not transfer funds. Retain the supplied partial-refund ID when reconciling uncertain outcomes.",
    "monetization.subscriptions.basePlans.migratePrices": "Migrates existing subscriber price cohorts before the explicit regional cutoff to current prices. Google notifies affected subscribers. Catalog readback cannot count the cohort or prove completion.",
    "monetization.subscriptions.basePlans.batchMigratePrices": "Requests price migration for each distinct base plan. Responses follow request order; do not infer atomicity or automatically retry subsets. Catalog readback cannot count subscribers or prove completed migration.",
}
PURCHASE_METHODS = frozenset(PREFIX + name for name in EFFECTS)
# Local ceiling interprets the documented one-year limit conservatively as 365 days.
MIN_DEFERRAL_SECONDS = 86400
MAX_DEFERRAL_SECONDS = 365 * MIN_DEFERRAL_SECONDS
_REFERENCE_KEY = secrets.token_bytes(32)


def _text(value, *, limit=4096, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (not empty and not value.strip())
        or any(ord(c) < 32 and c not in "\r\n\t" for c in value)
    ):
        raise PlayError("Purchase intent requires bounded explicit text.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PlayError("Purchase intent must be valid Unicode.") from None
    return value


def _segment(value, *, limit=1024):
    _text(value, limit=limit)
    if value in {".", ".."} or any(c in value for c in "/\\%?#") or any(c.isspace() for c in value):
        raise PlayError("Purchase resource identifiers must be unencoded bounded path segments.")
    return quote(value, safe="")


def _package(value):
    if not isinstance(value, str) or len(value) > 255 or not PACKAGE.fullmatch(value):
        raise PlayError("Invalid Android package name.")
    return value


def _external_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", value):
        raise PlayError("External transaction identities must be bounded unencoded identifiers.")
    return value


def int64(value, *, minimum=0):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"0|[1-9][0-9]{0,18}", value)
        or not minimum <= int(value) <= 2**63 - 1
    ):
        raise PlayError("Expected a canonical decimal int64 string within the permitted range.")
    return int(value)


def validate_timestamp(value):
    """Validate RFC 3339 without rewriting operator precision or time zone."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
        value,
    ):
        raise PlayError("Expected an explicit RFC 3339 timestamp with a timezone.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PlayError("Expected a valid RFC 3339 timestamp.") from None
    return result


def validate_price(value):
    """Return currency and exact integer micros; never use floating-point money."""
    if not isinstance(value, dict) or set(value) != {"currency", "priceMicros"}:
        raise PlayError("Prices require explicit currency and integer micros.")
    if not isinstance(value["currency"], str) or not re.fullmatch(r"[A-Z]{3}", value["currency"]):
        raise PlayError("Prices require an uppercase ISO currency code.")
    return value["currency"], int64(value["priceMicros"])


def _oneof(value, fields, *, optional=False):
    count = len(set(value) & set(fields))
    if count > 1 or (not optional and count != 1):
        raise PlayError("Choose exactly one explicit action or transaction variant.")


def _country(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{2}", value):
        raise PlayError("Expected an explicit uppercase two-letter country code.")


def _fields(value, node):
    if node.get("readOnly"):
        raise PlayError("Output-only fields cannot be supplied as purchase write intent.")
    if "$ref" in node:
        node = definitions()["androidpublisher"]["schemas"][node["$ref"]]
    if isinstance(value, dict):
        properties = node.get("properties", {})
        for key, child in properties.items():
            if child.get("description", "").startswith("Required.") and key not in value:
                raise PlayError("Purchase intent is missing a required provider field.")
        for key, child in value.items():
            _fields(child, properties.get(key, {}))
    elif isinstance(value, list):
        for child in value:
            _fields(child, node.get("items", {}))
    elif node.get("type") == "integer" and type(value) is not int:
        raise PlayError("Integer fields require JSON integers, not booleans or floats.")
    elif isinstance(value, str):
        _text(value, limit=65536, empty=True)
        if node.get("format") == "google-datetime":
            validate_timestamp(value)
        if "enum" in node and value.endswith("_UNSPECIFIED"):
            raise PlayError("Choose an explicit non-unspecified provider enum value.")


def package_for_spec(spec):
    p = spec["parameters"]
    if "packageName" in p:
        return _package(p["packageName"])
    parts = p.get("parent", p.get("name", "")).split("/")
    if len(parts) not in {2, 4} or parts[0] != "applications":
        raise PlayError("External transaction parent must identify one explicit application.")
    return _package(parts[1])


def _migration(item, parent):
    for field in ("packageName", "productId", "basePlanId"):
        identifier(item.get(field), field)
        if field in parent and parent[field] != "-" and item[field] != parent[field]:
            raise PlayError("Price migration identity is outside the declared parent.")
    _text(item.get("regionsVersion", {}).get("version"), limit=128)
    regions = item.get("regionalPriceMigrations")
    if not isinstance(regions, list) or not regions:
        raise PlayError("Price migration requires explicit regional intent.")
    seen = set()
    for region in regions:
        _country(region.get("regionCode"))
        if region["regionCode"] in seen:
            raise PlayError("Price migration regions must be distinct.")
        seen.add(region["regionCode"])
        validate_timestamp(region.get("oldestAllowedPriceVersionTime"))
        if region.get("priceIncreaseType") not in {
            "PRICE_INCREASE_TYPE_OPT_IN",
            "PRICE_INCREASE_TYPE_OPT_OUT",
        }:
            raise PlayError("Select the intended opt-in or opt-out price increase behavior.")


def _external_create(parameters, body):
    transaction_id = _external_id(parameters.get("externalTransactionId"))
    currencies = {
        validate_price(body.get(key))[0] for key in ("originalPreTaxAmount", "originalTaxAmount")
    }
    if len(currencies) != 1:
        raise PlayError("External transaction amounts must use the same currency.")
    validate_timestamp(body.get("transactionTime"))
    address = body.get("userTaxAddress", {})
    _country(address.get("regionCode"))
    if address["regionCode"] == "IN":
        _text(address.get("administrativeArea"), limit=100)
    _oneof(body, ("oneTimeTransaction", "recurringTransaction"))
    _oneof(body, ("externalOfferDetails", "externalContentLinkDetails"), optional=True)
    if "oneTimeTransaction" in body:
        _text(body["oneTimeTransaction"].get("externalTransactionToken"))
    else:
        recurring = body["recurringTransaction"]
        _oneof(
            recurring,
            (
                "externalTransactionToken",
                "initialExternalTransactionId",
                "migratedTransactionProgram",
            ),
        )
        _oneof(recurring, ("externalSubscription", "otherRecurringProduct"))
        if "migratedTransactionProgram" in recurring and "externalSubscription" not in recurring:
            raise PlayError("Manual-reporting migration is only supported for subscriptions.")
        if "externalTransactionToken" in recurring:
            _text(recurring["externalTransactionToken"])
        if "initialExternalTransactionId" in recurring:
            if _external_id(recurring["initialExternalTransactionId"]) == transaction_id:
                raise PlayError(
                    "A recurring transaction cannot name itself as the initial transaction."
                )
    for field, category in (
        ("externalOfferDetails", "installedAppCategory"),
        ("externalContentLinkDetails", "externalAppCategory"),
    ):
        if field not in body:
            continue
        details = body[field]
        if field == "externalOfferDetails" and "transactionProgramCode" in body:
            raise PlayError("External offers cannot include transactionProgramCode.")
        if details.get("linkType") == "LINK_TO_APP_DOWNLOAD":
            _package(details.get("installedAppPackage"))
            if "oneTimeTransaction" not in body or any(
                validate_price(body[key])[1] != 0
                for key in ("originalPreTaxAmount", "originalTaxAmount")
            ):
                raise PlayError("App download reports must be zero-cost one-time transactions.")
            if details.get(category) not in {"APP", "GAME"}:
                raise PlayError("App download reporting requires an explicit app category.")
        elif "installedAppPackage" in details or category in details:
            raise PlayError("Installed-app fields require an app-download link type.")
        if "appDownloadEventExternalTransactionId" in details:
            linked = _external_id(details["appDownloadEventExternalTransactionId"])
            if linked == transaction_id:
                raise PlayError("An external transaction cannot name itself as its download event.")
        if not details.get("linkType") and not details.get("appDownloadEventExternalTransactionId"):
            raise PlayError(
                "External offer reporting requires a link type or associated download event."
            )


def _review(body):
    _text(body.get("pendingRefundToken"))
    if (
        body.get("refundPreference") not in {"APPROVE", "DECLINE", "NEUTRAL"}
        or type(body.get("sampleContentProvided")) is not bool
    ):
        raise PlayError("Refund review requires explicit preference and sample-content boolean.")
    consumption = body.get("consumptionPercentageMilliunits", 0)
    if type(consumption) is not int or not 0 <= consumption <= 100000:
        raise PlayError(
            "Consumption percentage must be an integer from zero through 100000 milliunits."
        )
    events = body.get("consumptionUsageEvents", [])
    if len(events) > 1000:
        raise PlayError("Refund review supports at most 1000 usage events.")
    for event in events:
        if not event:
            raise PlayError("Usage evidence must contain explicit operator-supplied facts.")
        if "consumptionItemDescription" in event:
            _text(event["consumptionItemDescription"], limit=5000, empty=True)
        if "ipAddress" in event:
            try:
                ipaddress.ip_address(event["ipAddress"])
            except ValueError:
                raise PlayError("Usage evidence contains an invalid IP address.") from None
        if "location" in event:
            region = event["location"].get("regionCode")
            if not isinstance(region, str) or not re.fullmatch(r"[A-Z]{2}|[0-9]{3}", region):
                raise PlayError("Usage location requires an explicit CLDR region code.")
        for key in ("obfuscatedAccountId", "obfuscatedProfileId"):
            if key in event:
                _text(event[key], limit=64)


def request_spec(method_id, parameters, body=None):
    if not isinstance(method_id, str) or method_id not in PURCHASE_METHODS:
        raise PlayError("Method is outside the implemented purchase lifecycle capability.")
    validate_request(method_id, parameters, body)
    definition = methods()[method_id]
    if "request" in definition:
        _fields(body, definition["request"])
    elif body is not None:
        raise PlayError("This purchase action requires an absent request body.")
    parameters, body = deepcopy(parameters), deepcopy(body)
    spec = {"method": method_id, "parameters": parameters, "body": body}
    package_for_spec(spec)
    path, query = definition["path"], {}
    for key, value in parameters.items():
        if key in {"parent", "name"}:
            parts = value.split("/")
            if key == "name" and (len(parts) != 4 or parts[2] != "externalTransactions"):
                raise PlayError(
                    "External transaction name must identify one application and transaction."
                )
            if key == "parent" and len(parts) != 2:
                raise PlayError("External transaction parent must identify one application.")
            if key == "name":
                _external_id(parts[3])
            encoded = "/".join(quote(part, safe="") for part in parts)
            path = path.replace("{+" + key + "}", encoded)
        elif definition["parameters"][key]["location"] == "path":
            if key in {"productId", "basePlanId"} and method_id.startswith(BASE):
                identifier(
                    value,
                    key,
                    wildcard=method_id.endswith(".batchMigratePrices") and key == "productId",
                )
            path = path.replace(
                "{" + key + "}", _segment(value, limit=4096 if key == "token" else 1024)
            )
        else:
            query[key] = str(value).lower() if type(value) is bool else value
    name = method_id.removeprefix(PREFIX)
    if name.endswith(".acknowledge"):
        for value in (body or {}).get("externalAccountIds", {}).values():
            _text(value, limit=64)
    elif name == "purchases.subscriptions.defer":
        info = body.get("deferralInfo", {})
        extension = int64(info.get("desiredExpiryTimeMillis"), minimum=1) - int64(
            info.get("expectedExpiryTimeMillis"), minimum=1
        )
        if not MIN_DEFERRAL_SECONDS * 1000 <= extension <= MAX_DEFERRAL_SECONDS * 1000:
            raise PlayError(
                "Deferral must extend the explicit expected expiry by one through 365 days."
            )
    elif name == "purchases.subscriptionsv2.defer":
        context = body["deferralContext"]
        _text(context.get("etag"))
        duration = context.get("deferDuration")
        match = re.fullmatch(r"(0|[1-9][0-9]{0,11})(?:\.([0-9]{1,9}))?s", duration or "")
        if not match:
            raise PlayError("Deferral requires a positive bounded protobuf duration.")
        nanos = int(match[1]) * 10**9 + int((match[2] or "").ljust(9, "0"))
        if not MIN_DEFERRAL_SECONDS * 10**9 <= nanos <= MAX_DEFERRAL_SECONDS * 10**9:
            raise PlayError("Deferral duration must be between one and 365 days.")
    elif name == "purchases.subscriptionsv2.revoke":
        context = body["revocationContext"]
        _oneof(context, ("fullRefund", "proratedRefund", "itemBasedRefund"))
        if "itemBasedRefund" in context:
            _segment(context["itemBasedRefund"].get("productId"))
    elif name == "orders.refund":
        if type(parameters.get("revoke")) is not bool:
            raise PlayError("Order refunds require an explicit revoke boolean.")
    elif name == "orders.reviewrefund":
        _review(body)
    elif name == "externaltransactions.createexternaltransaction":
        _external_create(parameters, body)
    elif name == "externaltransactions.refundexternaltransaction":
        _oneof(body, ("fullRefund", "partialRefund"))
        validate_timestamp(body.get("refundTime"))
        if "partialRefund" in body:
            partial = body["partialRefund"]
            _segment(partial.get("refundId"))
            if validate_price(partial.get("refundPreTaxAmount"))[1] <= 0:
                raise PlayError("Partial refunds require a positive pre-tax amount.")
    elif method_id.startswith(BASE):
        if method_id.endswith(".batchMigratePrices"):
            rows = body.get("requests")
            if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
                raise PlayError("Price migration batches require one through 100 requests.")
        else:
            rows = [body]
        targets = set()
        for row in rows:
            _migration(row, parameters)
            target = tuple(row[field] for field in ("packageName", "productId", "basePlanId"))
            if target in targets:
                raise PlayError("Price migration batch targets must be distinct base plans.")
            targets.add(target)
    response = definition.get("response")
    empty = not response or not definitions()["androidpublisher"]["schemas"][response["$ref"]].get(
        "properties"
    )
    spec.update(
        http_method=definition["httpMethod"],
        url="https://androidpublisher.googleapis.com/"
        + path
        + ("?" + urlencode(query) if query else ""),
        empty_response=empty,
        effects=[EFFECTS[name]],
        validate_only=name == "purchases.subscriptionsv2.defer"
        and body["deferralContext"].get("validateOnly") is True,
    )
    return spec


@lru_cache(maxsize=14)
def _response_validator(method_id):
    schema = json_schema(methods()[method_id]["response"])
    schema["$defs"] = {
        key: json_schema(value)
        for key, value in definitions()["androidpublisher"]["schemas"].items()
    }
    return Draft202012Validator(schema)


def validate_mutation_response(spec, data):
    if not isinstance(data, dict):
        raise PlayError("Purchase response is inconclusive; outcome is unknown.")
    try:
        json.dumps(data, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PlayError("Purchase response is inconclusive; outcome is unknown.") from None
    if spec["empty_response"]:
        if data:
            raise PlayError("Expected an empty purchase action response; outcome is unknown.")
        return
    method = spec["method"]
    if not _response_validator(method).is_valid(data):
        raise PlayError("Purchase response violates the provider contract; outcome is unknown.")
    if method == PREFIX + "purchases.subscriptions.defer":
        if int64(data.get("newExpiryTimeMillis"), minimum=1) != int64(
            spec["body"]["deferralInfo"]["desiredExpiryTimeMillis"], minimum=1
        ):
            raise PlayError("Deferral response does not confirm the requested expiry.")
    elif method == PREFIX + "purchases.subscriptionsv2.defer":
        items = data.get("itemExpiryTimeDetails")
        if not isinstance(items, list) or not items or len(items) > 1000:
            raise PlayError("Deferral response lacks explicit subscription item expiries.")
        seen = set()
        for item in items:
            _segment(item.get("productId"))
            if item["productId"] in seen:
                raise PlayError("Deferral response contains duplicate subscription items.")
            seen.add(item["productId"])
            validate_timestamp(item.get("expiryTime"))
    elif method.startswith(EXTERNAL):
        parameters = spec["parameters"]
        expected_id = parameters.get("externalTransactionId") or parameters["name"].split("/")[3]
        if (
            data.get("packageName") != package_for_spec(spec)
            or data.get("externalTransactionId") != expected_id
        ):
            raise PlayError(
                "External transaction response identity differs from the requested resource."
            )
        if data.get("transactionState") not in {"TRANSACTION_REPORTED", "TRANSACTION_CANCELED"}:
            raise PlayError("External transaction response lacks an explicit state.")
        amounts = [
            validate_price(data.get(field))
            for field in (
                "originalPreTaxAmount",
                "originalTaxAmount",
                "currentPreTaxAmount",
                "currentTaxAmount",
            )
        ]
        if (
            len({currency for currency, amount in amounts}) != 1
            or amounts[2][1] > amounts[0][1]
            or amounts[3][1] > amounts[1][1]
        ):
            raise PlayError("External transaction response contains inconsistent amounts.")
        if method.endswith(".createexternaltransaction"):
            for field in ("originalPreTaxAmount", "originalTaxAmount"):
                if data[field] != spec["body"][field]:
                    raise PlayError("External transaction response amounts differ from the report.")
        if "fullRefund" in spec["body"] and (
            data["transactionState"] != "TRANSACTION_CANCELED" or amounts[2][1] or amounts[3][1]
        ):
            raise PlayError(
                "External transaction response does not confirm the full refund report."
            )
    elif method == BASE + "batchMigratePrices":
        if data.get("responses") != [{} for _ in spec["body"]["requests"]]:
            raise PlayError("Migration response does not account for every ordered request.")


def opaque_reference(value):
    """Process-local reference, without exposing guessable customer identifiers."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "target_" + hmac.new(_REFERENCE_KEY, encoded, hashlib.sha256).hexdigest()[:24]


def redacted_preview(spec):
    p, body = spec["parameters"], spec["body"] or {}
    method = spec["method"]
    intent = {}
    if method.startswith(BASE):
        intent = deepcopy(body)
    elif method == PREFIX + "orders.reviewrefund":
        intent = {
            key: body[key]
            for key in (
                "refundPreference",
                "sampleContentProvided",
                "consumptionPercentageMilliunits",
            )
            if key in body
        }
        intent["usage_event_count"] = len(body.get("consumptionUsageEvents", []))
    elif method.startswith(EXTERNAL):
        for key in (
            "originalPreTaxAmount",
            "originalTaxAmount",
            "transactionTime",
            "refundTime",
            "transactionProgramCode",
        ):
            if key in body:
                intent[key] = deepcopy(body[key])
        for field in ("externalOfferDetails", "externalContentLinkDetails"):
            if field in body:
                details = body[field]
                intent[field] = {
                    key: details[key]
                    for key in (
                        "linkType",
                        "installedAppPackage",
                        "installedAppCategory",
                        "externalAppCategory",
                    )
                    if key in details
                }
                if "appDownloadEventExternalTransactionId" in details:
                    intent[field]["download_event_reference"] = opaque_reference(
                        details["appDownloadEventExternalTransactionId"]
                    )
        if "partialRefund" in body:
            intent["partialRefund"] = {
                "refundPreTaxAmount": deepcopy(body["partialRefund"]["refundPreTaxAmount"]),
                "refund_reference": opaque_reference(body["partialRefund"]["refundId"]),
            }
        if "fullRefund" in body:
            intent["fullRefund"] = {}
        if "recurringTransaction" in body:
            recurring = body["recurringTransaction"]
            intent["transaction_type"] = "recurring"
            intent["recurring_product_type"] = (
                "subscription" if "externalSubscription" in recurring else "other"
            )
            if "externalSubscription" in recurring:
                intent["subscription_type"] = recurring["externalSubscription"]["subscriptionType"]
            if "migratedTransactionProgram" in recurring:
                intent["migratedTransactionProgram"] = recurring["migratedTransactionProgram"]
            if "initialExternalTransactionId" in recurring:
                intent["initial_transaction_reference"] = opaque_reference(
                    recurring["initialExternalTransactionId"]
                )
        elif "oneTimeTransaction" in body:
            intent["transaction_type"] = "one_time"
    else:
        for key in ("cancellationContext", "revocationContext", "deferralInfo"):
            if key in body:
                intent[key] = deepcopy(body[key])
        if "deferralContext" in body:
            intent["deferralContext"] = {
                "deferDuration": body["deferralContext"]["deferDuration"],
                "validateOnly": spec["validate_only"],
                "etag_supplied": True,
            }
        if "revoke" in p:
            intent["revoke"] = p["revoke"]
    return {
        "method": method,
        "packageName": package_for_spec(spec),
        "target_reference": opaque_reference(p),
        "target": {
            key: p[key] for key in ("productId", "subscriptionId", "basePlanId") if key in p
        },
        "intent": intent,
        "effects": deepcopy(spec["effects"]),
        "validate_only": spec["validate_only"],
        "redaction_note": "Purchase and refund tokens, account identifiers, customer addresses, usage details, developer payloads and token-bearing URLs are omitted. Exact requests remain private in the process-memory preparation.",
    }


def redacted_response(spec, data):
    """Allowlisted response details only; never expose the raw provider resource."""
    validate_mutation_response(spec, data)
    method = spec["method"]
    if spec["empty_response"]:
        return {"provider_accepted": True}
    if method == PREFIX + "purchases.subscriptions.defer":
        return {"newExpiryTimeMillis": data["newExpiryTimeMillis"]}
    if method == PREFIX + "purchases.subscriptionsv2.defer":
        return {"itemExpiryTimeDetails": deepcopy(data["itemExpiryTimeDetails"])}
    if method.startswith(EXTERNAL):
        return {
            "transaction_reference": opaque_reference(
                {
                    "packageName": data["packageName"],
                    "externalTransactionId": data["externalTransactionId"],
                }
            ),
            **{
                key: deepcopy(data[key])
                for key in (
                    "transactionState",
                    "originalPreTaxAmount",
                    "originalTaxAmount",
                    "currentPreTaxAmount",
                    "currentTaxAmount",
                )
            },
        }
    return {"response_count": len(data["responses"]), "ordered_responses_complete": True}
