"""Synthetic purchase contracts: fixed routes, financial intent and redaction."""

import json
from copy import deepcopy

import pytest

from pubship.errors import PlayError
from pubship.purchase_contracts import (
    PURCHASE_METHODS,
    int64,
    package_for_spec,
    redacted_preview,
    redacted_response,
    request_spec,
    validate_mutation_response,
    validate_price,
    validate_timestamp,
)

P = "androidpublisher."
BASE = "monetization.subscriptions.basePlans."
PACKAGE = {"packageName": "com.example.app"}
TOKEN = "PRIVATE_PURCHASE_TOKEN"
PRODUCT = {**PACKAGE, "productId": "coins", "token": TOKEN}
LEGACY = {**PACKAGE, "subscriptionId": "premium", "token": TOKEN}
V2 = {**PACKAGE, "token": TOKEN}
ORDER = {**PACKAGE, "orderId": "PRIVATE_ORDER_ID"}
PRICE = {"currency": "EUR", "priceMicros": "1234567"}
ZERO = {"currency": "EUR", "priceMicros": "0"}
TIME = "2026-01-01T12:34:56.123456789Z"
MIGRATION = {
    **PACKAGE,
    "productId": "premium",
    "basePlanId": "monthly",
    "regionsVersion": {"version": "2022/02"},
    "regionalPriceMigrations": [
        {
            "regionCode": "US",
            "oldestAllowedPriceVersionTime": TIME,
            "priceIncreaseType": "PRICE_INCREASE_TYPE_OPT_IN",
        }
    ],
}
EXTERNAL_BODY = {
    "originalPreTaxAmount": PRICE,
    "originalTaxAmount": ZERO,
    "transactionTime": TIME,
    "oneTimeTransaction": {"externalTransactionToken": "PRIVATE_EXTERNAL_TOKEN"},
    "userTaxAddress": {"regionCode": "US", "administrativeArea": "PRIVATE_ADDRESS"},
}
EXT_ID = "PRIVATE_TRANSACTION_ID"
EXTERNAL_CREATE = {"parent": "applications/com.example.app", "externalTransactionId": EXT_ID}
EXTERNAL_REFUND = {"name": "applications/com.example.app/externalTransactions/" + EXT_ID}
REVIEW = {
    "pendingRefundToken": "PRIVATE_PENDING_TOKEN",
    "refundPreference": "DECLINE",
    "sampleContentProvided": False,
    "consumptionPercentageMilliunits": 45200,
    "consumptionUsageEvents": [
        {
            "consumptionItemDescription": "PRIVATE_DESCRIPTION",
            "consumptionTime": TIME,
            "ipAddress": "192.0.2.1",
            "obfuscatedAccountId": "PRIVATE_ACCOUNT",
            "location": {"regionCode": "US", "locality": "PRIVATE_LOCATION"},
        }
    ],
}
CASES = [
    (
        "purchases.products.acknowledge",
        PRODUCT,
        {"developerPayload": "PRIVATE_PAYLOAD"},
        "/purchases/products/coins/tokens/" + TOKEN + ":acknowledge",
        True,
    ),
    (
        "purchases.products.consume",
        PRODUCT,
        None,
        "/purchases/products/coins/tokens/" + TOKEN + ":consume",
        True,
    ),
    (
        "purchases.subscriptions.acknowledge",
        LEGACY,
        {"externalAccountIds": {"obfuscatedAccountId": "PRIVATE_ACCOUNT"}},
        "/purchases/subscriptions/premium/tokens/" + TOKEN + ":acknowledge",
        True,
    ),
    (
        "purchases.subscriptions.cancel",
        LEGACY,
        None,
        "/purchases/subscriptions/premium/tokens/" + TOKEN + ":cancel",
        True,
    ),
    (
        "purchases.subscriptions.defer",
        LEGACY,
        {
            "deferralInfo": {
                "expectedExpiryTimeMillis": "1700000000000",
                "desiredExpiryTimeMillis": "1700086400000",
            }
        },
        "/purchases/subscriptions/premium/tokens/" + TOKEN + ":defer",
        False,
    ),
    (
        "purchases.subscriptionsv2.cancel",
        V2,
        {"cancellationContext": {"cancellationType": "USER_REQUESTED_STOP_RENEWALS"}},
        "/purchases/subscriptionsv2/tokens/" + TOKEN + ":cancel",
        True,
    ),
    (
        "purchases.subscriptionsv2.defer",
        V2,
        {
            "deferralContext": {
                "etag": "PRIVATE_ETAG",
                "deferDuration": "86400s",
                "validateOnly": False,
            }
        },
        "/purchases/subscriptionsv2/tokens/" + TOKEN + ":defer",
        False,
    ),
    (
        "purchases.subscriptionsv2.revoke",
        V2,
        {"revocationContext": {"fullRefund": {}}},
        "/purchases/subscriptionsv2/tokens/" + TOKEN + ":revoke",
        True,
    ),
    (
        "orders.refund",
        {**ORDER, "revoke": False},
        None,
        "/orders/PRIVATE_ORDER_ID:refund?revoke=false",
        True,
    ),
    ("orders.reviewrefund", ORDER, REVIEW, "/orders/PRIVATE_ORDER_ID:reviewrefund", True),
    (
        "externaltransactions.createexternaltransaction",
        EXTERNAL_CREATE,
        EXTERNAL_BODY,
        "/externalTransactions?externalTransactionId=" + EXT_ID,
        False,
    ),
    (
        "externaltransactions.refundexternaltransaction",
        EXTERNAL_REFUND,
        {
            "refundTime": TIME,
            "partialRefund": {"refundId": "PRIVATE_REFUND_ID", "refundPreTaxAmount": PRICE},
        },
        "/externalTransactions/" + EXT_ID + ":refund",
        False,
    ),
    (
        BASE + "migratePrices",
        {**PACKAGE, "productId": "premium", "basePlanId": "monthly"},
        MIGRATION,
        "/subscriptions/premium/basePlans/monthly:migratePrices",
        True,
    ),
    (
        BASE + "batchMigratePrices",
        {**PACKAGE, "productId": "-"},
        {"requests": [MIGRATION]},
        "/subscriptions/-/basePlans:batchMigratePrices",
        False,
    ),
]


def case_spec(index):
    name, parameters, body, _, _ = CASES[index]
    return request_spec(P + name, deepcopy(parameters), deepcopy(body))


@pytest.mark.parametrize("name,parameters,body,suffix,empty", CASES)
def test_fourteen_fixed_routes_and_exact_intent(name, parameters, body, suffix, empty):
    incoming_parameters, incoming_body = deepcopy(parameters), deepcopy(body)
    spec = request_spec(P + name, incoming_parameters, incoming_body)
    assert (
        spec["url"]
        == "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/com.example.app"
        + suffix
    )
    assert spec["http_method"] == "POST"
    assert spec["parameters"] == parameters
    assert spec["body"] == body
    assert spec["empty_response"] is empty
    assert spec["effects"]
    assert package_for_spec(spec) == PACKAGE["packageName"]
    incoming_parameters["extra"] = True
    if incoming_body is not None:
        incoming_body["extra"] = True
    assert spec["parameters"] == parameters
    assert spec["body"] == body


def test_capability_is_exactly_fourteen():
    assert PURCHASE_METHODS == {P + row[0] for row in CASES}
    for name in (None, [], P + "orders.get", "https://example.org", P + "edits.commit"):
        with pytest.raises(PlayError):
            request_spec(name, PACKAGE, {})


@pytest.mark.parametrize("name,parameters,body,suffix,empty", CASES)
def test_unknown_fields_and_body_requirements(name, parameters, body, suffix, empty):
    with pytest.raises(PlayError):
        request_spec(P + name, {**parameters, "url": "PRIVATE_URL"}, body)
    with pytest.raises(PlayError):
        request_spec(P + name, parameters, {} if body is None else None)
    if body is not None:
        with pytest.raises(PlayError):
            request_spec(P + name, parameters, {**body, "unknown": "PRIVATE_VALUE"})


@pytest.mark.parametrize(
    "token", ["", "..", "a/b", "a%2Fb", "x?token=y", "x#y", "x\ny", "x" * 4097, "\ud800"]
)
def test_purchase_token_route_boundary(token):
    with pytest.raises(PlayError) as error:
        request_spec(P + "purchases.products.consume", {**PRODUCT, "token": token}, None)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "name",
    [
        "purchases.subscriptions.acknowledge",
        "purchases.subscriptions.cancel",
        "purchases.subscriptions.defer",
    ],
)
def test_legacy_subscription_identity_is_explicit(name):
    body = next(row[2] for row in CASES if row[0] == name)
    with pytest.raises(PlayError):
        request_spec(P + name, V2, body)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"cancellationContext": {}},
        {"cancellationContext": {"cancellationType": "CANCELLATION_TYPE_UNSPECIFIED"}},
    ],
)
def test_v2_cancellation_requires_explicit_semantics(body):
    with pytest.raises(PlayError):
        request_spec(P + "purchases.subscriptionsv2.cancel", V2, body)


@pytest.mark.parametrize(
    "duration",
    [
        "",
        "0s",
        "0.000000000s",
        "-1s",
        "01s",
        "P1D",
        "1.1234567890s",
        "315576000001s",
        "315576000000.1s",
    ],
)
def test_deferral_duration_bounds(duration):
    with pytest.raises(PlayError):
        request_spec(
            P + "purchases.subscriptionsv2.defer",
            V2,
            {"deferralContext": {"etag": "etag", "deferDuration": duration}},
        )


def test_v2_deferral_preserves_etag_and_validate_only():
    body = {
        "deferralContext": {
            "etag": "PRIVATE_ETAG",
            "deferDuration": "86400.000000001s",
            "validateOnly": True,
        }
    }
    spec = request_spec(P + "purchases.subscriptionsv2.defer", V2, body)
    assert spec["validate_only"] is True
    assert spec["body"] == body
    assert "PRIVATE_ETAG" not in json.dumps(redacted_preview(spec))
    for context in ({"deferDuration": "1s"}, {"etag": "", "deferDuration": "1s"}, {"etag": "etag"}):
        with pytest.raises(PlayError):
            request_spec(P + "purchases.subscriptionsv2.defer", V2, {"deferralContext": context})


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"expectedExpiryTimeMillis": "10"},
        {"expectedExpiryTimeMillis": "10", "desiredExpiryTimeMillis": "10"},
        {"expectedExpiryTimeMillis": "10", "desiredExpiryTimeMillis": "9"},
        {"expectedExpiryTimeMillis": "-1", "desiredExpiryTimeMillis": "20"},
    ],
)
def test_legacy_native_expiry_guard(info):
    with pytest.raises(PlayError):
        request_spec(P + "purchases.subscriptions.defer", LEGACY, {"deferralInfo": info})


@pytest.mark.parametrize(
    "context",
    [
        {},
        {"fullRefund": {}, "proratedRefund": {}},
        {"itemBasedRefund": {}},
        {"itemBasedRefund": {"productId": "other/product"}},
        {"itemBasedRefund": {"productId": "addon", "packageName": "com.other.app"}},
    ],
)
def test_revocation_unions_and_nested_identity(context):
    with pytest.raises(PlayError):
        request_spec(P + "purchases.subscriptionsv2.revoke", V2, {"revocationContext": context})


@pytest.mark.parametrize(
    "context",
    [{"fullRefund": {}}, {"proratedRefund": {}}, {"itemBasedRefund": {"productId": "addon"}}],
)
def test_revocation_modes(context):
    spec = request_spec(P + "purchases.subscriptionsv2.revoke", V2, {"revocationContext": context})
    assert redacted_preview(spec)["intent"]["revocationContext"] == context


@pytest.mark.parametrize("revoke", [None, 0, 1, "false"])
def test_order_refund_requires_boolean_choice(revoke):
    p = dict(ORDER)
    if revoke is not None:
        p["revoke"] = revoke
    with pytest.raises(PlayError):
        request_spec(P + "orders.refund", p, None)


@pytest.mark.parametrize(
    "change",
    [
        {"pendingRefundToken": ""},
        {"refundPreference": "REFUND_PREFERENCE_UNSPECIFIED"},
        {"sampleContentProvided": 0},
        {"consumptionPercentageMilliunits": -1},
        {"consumptionPercentageMilliunits": 100001},
        {"consumptionPercentageMilliunits": 1.0},
        {"consumptionUsageEvents": [{}]},
        {"consumptionUsageEvents": [{"consumptionItemDescription": "x" * 5001}]},
        {"consumptionUsageEvents": [{"ipAddress": "PRIVATE_INVALID_IP"}]},
        {"consumptionUsageEvents": [{"location": {}}]},
        {"consumptionUsageEvents": [{"consumptionTime": "2026-01-01"}]},
    ],
)
def test_review_refund_required_evidence_and_bounds(change):
    with pytest.raises(PlayError):
        request_spec(P + "orders.reviewrefund", ORDER, {**REVIEW, **change})


def test_review_event_count_boundary():
    body = {
        **REVIEW,
        "consumptionUsageEvents": [{"consumptionTime": "2026-01-01T00:00:00Z"}] * 1000,
    }
    request_spec(P + "orders.reviewrefund", ORDER, body)
    body["consumptionUsageEvents"].append(body["consumptionUsageEvents"][0])
    with pytest.raises(PlayError):
        request_spec(P + "orders.reviewrefund", ORDER, body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency", "usd"),
        ("currency", "EURO"),
        ("priceMicros", "1.5"),
        ("priceMicros", "01"),
        ("priceMicros", "-1"),
        ("priceMicros", 100),
        ("priceMicros", "9223372036854775808"),
    ],
)
def test_exact_micros_and_currency(field, value):
    with pytest.raises(PlayError):
        validate_price({**PRICE, field: value})


def test_integer_money_preserves_precision():
    assert validate_price({"currency": "EUR", "priceMicros": "9007199254740993"}) == (
        "EUR",
        9007199254740993,
    )
    assert int64("9223372036854775807") == 9223372036854775807


@pytest.mark.parametrize(
    "timestamp",
    [
        None,
        "2026-01-01",
        "2026-01-01T00:00:00",
        "2026-02-30T00:00:00Z",
        "2026-01-01T00:00:00.1234567890Z",
        "2026-01-01T00:00:00+25:00",
    ],
)
def test_timestamp_requires_explicit_valid_timezone(timestamp):
    with pytest.raises(PlayError):
        validate_timestamp(timestamp)


@pytest.mark.parametrize(
    "field",
    [
        "packageName",
        "externalTransactionId",
        "createTime",
        "currentPreTaxAmount",
        "currentTaxAmount",
        "testPurchase",
        "transactionState",
    ],
)
def test_external_output_only_fields_rejected(field):
    values = {
        "packageName": "com.other.app",
        "externalTransactionId": EXT_ID,
        "createTime": TIME,
        "currentPreTaxAmount": PRICE,
        "currentTaxAmount": ZERO,
        "testPurchase": {},
        "transactionState": "TRANSACTION_REPORTED",
    }
    with pytest.raises(PlayError):
        request_spec(
            P + "externaltransactions.createexternaltransaction",
            EXTERNAL_CREATE,
            {**EXTERNAL_BODY, field: values[field]},
        )


@pytest.mark.parametrize("id_value", ["", "a/b", "a%2Fb", "x" * 64, "user@example.org"])
def test_external_transaction_ids(id_value):
    with pytest.raises(PlayError):
        request_spec(
            P + "externaltransactions.createexternaltransaction",
            {**EXTERNAL_CREATE, "externalTransactionId": id_value},
            EXTERNAL_BODY,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"originalTaxAmount": {"currency": "USD", "priceMicros": "0"}},
        {"oneTimeTransaction": {}},
        {
            "recurringTransaction": {
                "externalTransactionToken": "token",
                "externalSubscription": {"subscriptionType": "RECURRING"},
            }
        },
        {"userTaxAddress": {"regionCode": "IN"}},
        {
            "externalOfferDetails": {},
            "externalContentLinkDetails": {"linkType": "LINK_TO_DIGITAL_CONTENT_OFFER"},
        },
        {"externalOfferDetails": {"linkType": "LINK_TO_APP_DOWNLOAD"}},
        {
            "externalOfferDetails": {"linkType": "LINK_TO_DIGITAL_CONTENT_OFFER"},
            "transactionProgramCode": 1,
        },
    ],
)
def test_external_creation_relationships(change):
    with pytest.raises(PlayError):
        request_spec(
            P + "externaltransactions.createexternaltransaction",
            EXTERNAL_CREATE,
            {**EXTERNAL_BODY, **change},
        )


@pytest.mark.parametrize(
    "recurring",
    [
        {},
        {"externalTransactionToken": "token"},
        {"initialExternalTransactionId": EXT_ID, "otherRecurringProduct": {}},
        {
            "initialExternalTransactionId": "other",
            "externalTransactionToken": "token",
            "otherRecurringProduct": {},
        },
        {
            "externalTransactionToken": "token",
            "otherRecurringProduct": {},
            "externalSubscription": {"subscriptionType": "RECURRING"},
        },
        {"externalTransactionToken": "token", "externalSubscription": {}},
        {
            "migratedTransactionProgram": "EXTERNAL_TRANSACTION_PROGRAM_UNSPECIFIED",
            "otherRecurringProduct": {},
        },
    ],
)
def test_recurring_transaction_unions(recurring):
    body = {k: deepcopy(v) for k, v in EXTERNAL_BODY.items() if k != "oneTimeTransaction"}
    body["recurringTransaction"] = recurring
    with pytest.raises(PlayError):
        request_spec(P + "externaltransactions.createexternaltransaction", EXTERNAL_CREATE, body)


@pytest.mark.parametrize(
    "recurring",
    [
        {
            "externalTransactionToken": "token",
            "externalSubscription": {"subscriptionType": "RECURRING"},
        },
        {"initialExternalTransactionId": "initial", "otherRecurringProduct": {}},
        {
            "migratedTransactionProgram": "USER_CHOICE_BILLING",
            "externalSubscription": {"subscriptionType": "PREPAID"},
        },
    ],
)
def test_recurring_initial_renewal_and_migration(recurring):
    body = {k: deepcopy(v) for k, v in EXTERNAL_BODY.items() if k != "oneTimeTransaction"}
    body["recurringTransaction"] = recurring
    spec = request_spec(P + "externaltransactions.createexternaltransaction", EXTERNAL_CREATE, body)
    assert spec["body"] == body


@pytest.mark.parametrize(
    "refund",
    [
        {},
        {"fullRefund": {}, "partialRefund": {"refundId": "id", "refundPreTaxAmount": PRICE}},
        {"partialRefund": {}},
        {"partialRefund": {"refundId": "id", "refundPreTaxAmount": ZERO}},
        {"partialRefund": {"refundId": "", "refundPreTaxAmount": PRICE}},
    ],
)
def test_external_refund_union(refund):
    with pytest.raises(PlayError):
        request_spec(
            P + "externaltransactions.refundexternaltransaction",
            EXTERNAL_REFUND,
            {"refundTime": TIME, **refund},
        )


@pytest.mark.parametrize(
    "change",
    [
        {"packageName": "com.other.app"},
        {"productId": "other"},
        {"basePlanId": "other"},
        {"regionsVersion": {}},
        {"regionalPriceMigrations": []},
        {"regionalPriceMigrations": [{"regionCode": "US", "oldestAllowedPriceVersionTime": TIME}]},
        {"regionalPriceMigrations": MIGRATION["regionalPriceMigrations"] * 2},
    ],
)
def test_migration_nested_identity_and_complete_intent(change):
    with pytest.raises(PlayError):
        request_spec(
            P + BASE + "migratePrices",
            {**PACKAGE, "productId": "premium", "basePlanId": "monthly"},
            {**MIGRATION, **change},
        )


def test_migration_batch_100_101_distinct_and_cross_app():
    parameters = {**PACKAGE, "productId": "-"}
    rows = [{**deepcopy(MIGRATION), "basePlanId": "plan-" + str(n)} for n in range(100)]
    spec = request_spec(P + BASE + "batchMigratePrices", parameters, {"requests": rows})
    validate_mutation_response(spec, {"responses": [{} for _ in rows]})
    for bad in (
        rows + [{**MIGRATION, "basePlanId": "last"}],
        [rows[0], rows[0]],
        [{**MIGRATION, "packageName": "com.other.app"}],
        [],
    ):
        with pytest.raises(PlayError):
            request_spec(P + BASE + "batchMigratePrices", parameters, {"requests": bad})
    for response in (
        {},
        {"responses": [{}] * 99},
        {"responses": [{}] * 101},
        {"responses": [{"status": "ok"}] * 100},
    ):
        with pytest.raises(PlayError):
            validate_mutation_response(spec, response)


@pytest.mark.parametrize("index", range(14))
def test_previews_redact_sensitive_input_and_routes(index):
    spec = case_spec(index)
    preview = redacted_preview(spec)
    encoded = json.dumps(preview)
    assert "PRIVATE_" not in encoded
    assert "https://" not in encoded
    assert "192.0.2.1" not in encoded
    assert preview["target_reference"].startswith("target_")
    assert preview["target_reference"] == redacted_preview(spec)["target_reference"]
    assert preview["effects"]


@pytest.mark.parametrize("index", [0, 1, 2, 3, 5, 7, 8, 9, 12])
def test_empty_success_and_malformed_responses(index):
    spec = case_spec(index)
    validate_mutation_response(spec, {})
    assert redacted_response(spec, {}) == {"provider_accepted": True}
    for response in (None, [], {"success": True}):
        with pytest.raises(PlayError):
            validate_mutation_response(spec, response)


def external_response():
    return {
        "packageName": "com.example.app",
        "externalTransactionId": EXT_ID,
        "transactionState": "TRANSACTION_REPORTED",
        "originalPreTaxAmount": PRICE,
        "originalTaxAmount": ZERO,
        "currentPreTaxAmount": PRICE,
        "currentTaxAmount": ZERO,
        "userTaxAddress": EXTERNAL_BODY["userTaxAddress"],
        "oneTimeTransaction": EXTERNAL_BODY["oneTimeTransaction"],
    }


def test_external_response_identity_amounts_and_redaction():
    spec = case_spec(10)
    response = external_response()
    validate_mutation_response(spec, response)
    assert "PRIVATE_" not in json.dumps(redacted_response(spec, response))
    for patch in (
        {"packageName": "com.other.app"},
        {"externalTransactionId": "other"},
        {"transactionState": "TRANSACTION_STATE_UNSPECIFIED"},
        {"originalPreTaxAmount": ZERO},
        {"currentPreTaxAmount": {"currency": "USD", "priceMicros": "1234567"}},
        {"currentPreTaxAmount": {"currency": "EUR", "priceMicros": "9999999"}},
    ):
        with pytest.raises(PlayError):
            validate_mutation_response(spec, {**response, **patch})


def test_full_external_refund_requires_zero_remaining_and_canceled():
    spec = request_spec(
        P + "externaltransactions.refundexternaltransaction",
        EXTERNAL_REFUND,
        {"refundTime": TIME, "fullRefund": {}},
    )
    with pytest.raises(PlayError):
        validate_mutation_response(spec, external_response())
    response = {
        **external_response(),
        "currentPreTaxAmount": ZERO,
        "transactionState": "TRANSACTION_CANCELED",
    }
    validate_mutation_response(spec, response)


def test_deferral_response_evidence():
    legacy = case_spec(4)
    validate_mutation_response(legacy, {"newExpiryTimeMillis": "1700086400000"})
    with pytest.raises(PlayError):
        validate_mutation_response(legacy, {"newExpiryTimeMillis": "1700086401000"})
    v2 = case_spec(6)
    valid = {"itemExpiryTimeDetails": [{"productId": "premium", "expiryTime": TIME}]}
    validate_mutation_response(v2, valid)
    assert redacted_response(v2, valid) == valid
    for response in (
        {},
        {"itemExpiryTimeDetails": []},
        {"itemExpiryTimeDetails": [{"productId": "premium"}]},
        {"itemExpiryTimeDetails": valid["itemExpiryTimeDetails"] * 2},
    ):
        with pytest.raises(PlayError):
            validate_mutation_response(v2, response)


def test_errors_do_not_interpolate_sensitive_content():
    for body in (
        {
            "pendingRefundToken": "PRIVATE_TOKEN",
            "refundPreference": "PRIVATE_BAD_ENUM",
            "sampleContentProvided": True,
        },
        {**REVIEW, "consumptionUsageEvents": [{"ipAddress": "PRIVATE_BAD_IP"}]},
    ):
        with pytest.raises(PlayError) as error:
            request_spec(P + "orders.reviewrefund", ORDER, body)
        assert "PRIVATE" not in str(error.value)


def test_invalid_timezone_minutes_are_not_normalized():
    with pytest.raises(PlayError):
        validate_timestamp("2026-01-01T00:00:00+01:99")


def test_external_app_download_requires_zero_cost_and_shows_program():
    body = {
        **EXTERNAL_BODY,
        "originalPreTaxAmount": ZERO,
        "externalOfferDetails": {
            "linkType": "LINK_TO_APP_DOWNLOAD",
            "installedAppPackage": "com.example.download",
            "installedAppCategory": "APP",
        },
    }
    spec = request_spec(P + "externaltransactions.createexternaltransaction", EXTERNAL_CREATE, body)
    assert redacted_preview(spec)["intent"]["externalOfferDetails"] == body["externalOfferDetails"]
    for patch in ({"originalPreTaxAmount": PRICE}, {"originalTaxAmount": PRICE}):
        with pytest.raises(PlayError):
            request_spec(
                P + "externaltransactions.createexternaltransaction",
                EXTERNAL_CREATE,
                {**body, **patch},
            )


def test_external_download_reference_redacted():
    body = {
        **EXTERNAL_BODY,
        "externalOfferDetails": {"appDownloadEventExternalTransactionId": "PRIVATE_DOWNLOAD_ID"},
    }
    spec = request_spec(P + "externaltransactions.createexternaltransaction", EXTERNAL_CREATE, body)
    preview = redacted_preview(spec)
    assert preview["intent"]["externalOfferDetails"]["download_event_reference"].startswith(
        "target_"
    )
    assert "PRIVATE_" not in json.dumps(preview)


def test_manual_external_migration_only_for_subscriptions():
    body = {
        key: deepcopy(value) for key, value in EXTERNAL_BODY.items() if key != "oneTimeTransaction"
    }
    body["recurringTransaction"] = {
        "migratedTransactionProgram": "USER_CHOICE_BILLING",
        "otherRecurringProduct": {},
    }
    with pytest.raises(PlayError):
        request_spec(P + "externaltransactions.createexternaltransaction", EXTERNAL_CREATE, body)


@pytest.mark.parametrize("duration", ["86399.999999999s", "31536000.000000001s"])
def test_v2_deferral_billing_duration_boundaries(duration):
    with pytest.raises(PlayError):
        request_spec(
            P + "purchases.subscriptionsv2.defer",
            V2,
            {"deferralContext": {"etag": "etag", "deferDuration": duration}},
        )


@pytest.mark.parametrize("duration", ["86400s", "31536000s"])
def test_v2_deferral_billing_duration_endpoints(duration):
    request_spec(
        P + "purchases.subscriptionsv2.defer",
        V2,
        {"deferralContext": {"etag": "etag", "deferDuration": duration}},
    )


@pytest.mark.parametrize("extension", [86399999, 31536000001])
def test_legacy_deferral_billing_duration_boundaries(extension):
    with pytest.raises(PlayError):
        request_spec(
            P + "purchases.subscriptions.defer",
            LEGACY,
            {
                "deferralInfo": {
                    "expectedExpiryTimeMillis": "1700000000000",
                    "desiredExpiryTimeMillis": str(1700000000000 + extension),
                }
            },
        )
