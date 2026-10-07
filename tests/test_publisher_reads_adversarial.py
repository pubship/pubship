"""Independent Publisher GET acceptance, using only synthetic provider data."""

import asyncio
import io
import json
import sys
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from pubship.config import Settings
from pubship.errors import PlayError
from pubship.publisher_reads import PublisherReads
from pubship.server import create_server

APP = "com.example.app"
STORE = "com.example.store"
PREFIX = "androidpublisher."
BASE = f"/androidpublisher/v3/applications/{APP}"
TOKEN = "PRIVATE-purchase-token"
PAGE = "next-page_1"

# Expected routes are written independently of the request builder and Discovery
# traversal so mistakes in either cannot manufacture their own passing oracle.
CASES = {
    "applications.deviceTierConfigs.get": (
        {"deviceTierConfigId": "9223372036854775807"},
        "/deviceTierConfigs/9223372036854775807",
        [],
    ),
    "applications.deviceTierConfigs.list": (
        {"pageSize": 100, "pageToken": PAGE},
        "/deviceTierConfigs",
        [("pageSize", "100"), ("pageToken", PAGE)],
    ),
    "apprecovery.list": ({"versionCode": "22"}, "/appRecoveries", [("versionCode", "22")]),
    "edits.expansionfiles.get": (
        {"editId": "edit-1", "apkVersionCode": 22, "expansionFileType": "main"},
        "/edits/edit-1/apks/22/expansionFiles/main",
        [],
    ),
    "edits.testers.get": (
        {"editId": "edit-1", "track": "wear:production"},
        "/edits/edit-1/testers/wear%3Aproduction",
        [],
    ),
    "generatedapks.list": ({"versionCode": 22}, "/generatedApks/22", []),
    "inappproducts.batchGet": (
        {"sku": ["sku.one", "sku.two"]},
        "/inappproducts:batchGet",
        [("sku", "sku.one"), ("sku", "sku.two")],
    ),
    "inappproducts.get": ({"sku": "sku.one"}, "/inappproducts/sku.one", []),
    "inappproducts.list": (
        {"maxResults": 100, "startIndex": 0, "token": PAGE},
        "/inappproducts",
        [("maxResults", "100"), ("startIndex", "0"), ("token", PAGE)],
    ),
    "monetization.onetimeproducts.batchGet": (
        {"productIds": ["product.one", "product.two"]},
        "/oneTimeProducts:batchGet",
        [("productIds", "product.one"), ("productIds", "product.two")],
    ),
    "monetization.onetimeproducts.get": (
        {"productId": "product.one"},
        "/oneTimeProducts/product.one",
        [],
    ),
    "monetization.onetimeproducts.list": (
        {"pageSize": 1000, "pageToken": PAGE},
        "/oneTimeProducts",
        [("pageSize", "1000"), ("pageToken", PAGE)],
    ),
    "monetization.onetimeproducts.purchaseOptions.offers.list": (
        {"productId": "-", "purchaseOptionId": "-", "pageSize": 1000},
        "/oneTimeProducts/-/purchaseOptions/-/offers",
        [("pageSize", "1000")],
    ),
    "monetization.subscriptions.basePlans.offers.get": (
        {"productId": "product.one", "basePlanId": "plan-one", "offerId": "offer-one"},
        "/subscriptions/product.one/basePlans/plan-one/offers/offer-one",
        [],
    ),
    "monetization.subscriptions.basePlans.offers.list": (
        {"productId": "-", "basePlanId": "-", "pageToken": PAGE},
        "/subscriptions/-/basePlans/-/offers",
        [("pageToken", PAGE)],
    ),
    "monetization.subscriptions.batchGet": (
        {"productIds": ["product.one", "product.two"]},
        "/subscriptions:batchGet",
        [("productIds", "product.one"), ("productIds", "product.two")],
    ),
    "monetization.subscriptions.get": (
        {"productId": "product.one"},
        "/subscriptions/product.one",
        [],
    ),
    "monetization.subscriptions.list": (
        {"showArchived": False, "pageSize": 1000},
        "/subscriptions",
        [("showArchived", "false"), ("pageSize", "1000")],
    ),
    "orders.batchget": (
        {"orderIds": ["GPA.1111-2222-3333-44444", "GPA.1111-2222-3333-44444..0"]},
        "/orders:batchGet",
        [("orderIds", "GPA.1111-2222-3333-44444"), ("orderIds", "GPA.1111-2222-3333-44444..0")],
    ),
    "orders.get": (
        {"orderId": "GPA.1111-2222-3333-44444..0"},
        "/orders/GPA.1111-2222-3333-44444..0",
        [],
    ),
    "purchases.products.get": (
        {"productId": "product.one", "token": TOKEN},
        "/purchases/products/product.one/tokens/" + TOKEN,
        [],
    ),
    "purchases.productsv2.getproductpurchasev2": (
        {"token": TOKEN},
        "/purchases/productsv2/tokens/" + TOKEN,
        [],
    ),
    "purchases.subscriptionsv2.get": (
        {"token": TOKEN},
        "/purchases/subscriptionsv2/tokens/" + TOKEN,
        [],
    ),
    "systemapks.variants.get": (
        {"versionCode": "22", "variantId": 4294967295},
        "/systemApks/22/variants/4294967295",
        [],
    ),
    "systemapks.variants.list": ({"versionCode": "22"}, "/systemApks/22/variants", []),
}
# External transactions embed their package in an expanded resource name.
SPECIAL = {
    "externaltransactions.getexternaltransaction": (
        {"name": f"applications/{APP}/externalTransactions/PRIVATE-external-1"},
        BASE + "/externalTransactions/PRIVATE-external-1",
        [],
    ),
}
# Correct the total below against the agreed release inventory, independently of
# catalog status, which may be regenerated later in the same delivery cycle.
SENSITIVE = {
    "edits.testers.get",
    "externaltransactions.getexternaltransaction",
    "orders.batchget",
    "orders.get",
    "purchases.products.get",
    "purchases.productsv2.getproductpurchasev2",
    "purchases.subscriptionsv2.get",
}


def configured(*, sensitive=True):
    return Settings(
        packages=frozenset({APP, STORE}),
        sensitive_read_packages=frozenset({APP, STORE}) if sensitive else frozenset(),
    )


def service():
    auth = Mock()
    auth.token.return_value = "PRIVATE-auth-token"
    return PublisherReads(configured(), auth, local=True), auth


def request_case(method):
    if method in SPECIAL:
        return SPECIAL[method]
    extra, suffix, query = CASES[method]
    return {"packageName": APP, **extra}, BASE + suffix, query


@pytest.mark.parametrize("method", [*CASES, *SPECIAL])
def test_each_method_uses_exact_get_route_query_and_preserves_provider_data(method, caplog):
    reader, auth = service()
    params, path, query = request_case(method)
    data = {
        "nextPageToken": "provider-next-token",
        "versionCode": "9223372036854775807",
        "futureState": "UNKNOWN_FUTURE",
        "items": [],
        "omittedIsNotZero": None,
        "downloadUrl": "https://untrusted.example.test/artifact",
    }
    if method in SENSITIVE:
        data["customerEmail"] = "synthetic-customer@example.test"
    with patch(
        "urllib.request.OpenerDirector.open", return_value=io.BytesIO(json.dumps(data).encode())
    ) as send:
        result = (reader.read_sensitive if method in SENSITIVE else reader.read)(
            PREFIX + method, params
        )
    auth.token.assert_called_once_with("publisher")
    send.assert_called_once()
    request = send.call_args.args[0]
    assert request.get_method() == "GET" and request.data is None
    assert request.get_header("Authorization") == "Bearer PRIVATE-auth-token"
    assert send.call_args.kwargs["timeout"] == 15
    url = urlsplit(request.full_url)
    assert url.scheme == "https" and url.netloc == "androidpublisher.googleapis.com"
    assert url.path == path and not url.fragment
    assert sorted(parse_qsl(url.query, keep_blank_values=True)) == sorted(query)
    assert result["data"] == data
    assert result["method"] == PREFIX + method
    assert result["sensitive"] is (method in SENSITIVE)
    assert "PRIVATE" not in json.dumps(result) + caplog.text
    assert "synthetic-customer" not in caplog.text
    assert "parameters" not in result and "url" not in result


@pytest.mark.parametrize("method", sorted(SENSITIVE))
def test_sensitive_methods_require_separate_capability_and_correct_entrypoint(method):
    params, _, _ = request_case(method)
    auth = Mock()
    with patch("pubship.http.get") as read:
        for reader, operation in [
            (PublisherReads(configured(), auth, local=True), "read"),
            (PublisherReads(configured(sensitive=False), auth, local=True), "read_sensitive"),
            (PublisherReads(configured(), auth), "read_sensitive"),
        ]:
            with pytest.raises(PlayError):
                getattr(reader, operation)(PREFIX + method, params)
    auth.token.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("method", [*CASES, *SPECIAL])
def test_every_method_rejects_unknown_app_before_auth(method):
    reader, auth = service()
    params = dict(request_case(method)[0])
    if "name" in params:
        params["name"] = "applications/com.other.app/externalTransactions/PRIVATE-transaction"
    elif "appStorePackageName" in params:
        params["appStorePackageName"] = "com.other.store"
    else:
        params["packageName"] = "com.other.app"
    with patch("pubship.http.get") as read, pytest.raises(PlayError):
        (reader.read_sensitive if method in SENSITIVE else reader.read)(PREFIX + method, params)
    auth.token.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("value", ["..", "x/y", "x%2Fy", "x\\y", "\ud800", "x\nPRIVATE"])
def test_resource_path_injection_rejected_before_auth(value):
    reader, auth = service()
    with patch("pubship.http.get") as read, pytest.raises(PlayError) as caught:
        reader.read(
            PREFIX + "monetization.subscriptions.get", {"packageName": APP, "productId": value}
        )
    auth.token.assert_not_called()
    read.assert_not_called()
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize(
    "method,extra",
    [
        ("applications.deviceTierConfigs.list", {"pageSize": True}),
        ("applications.deviceTierConfigs.list", {"pageSize": 101}),
        ("monetization.subscriptions.list", {"pageSize": 1001}),
        ("monetization.subscriptions.list", {"showArchived": "false"}),
        ("monetization.subscriptions.batchGet", {"productIds": []}),
        ("monetization.subscriptions.batchGet", {"productIds": ["duplicate", "duplicate"]}),
        ("monetization.subscriptions.batchGet", {"productIds": [str(i) for i in range(101)]}),
        ("monetization.subscriptions.batchGet", {"productIds": "not-an-array"}),
        (
            "monetization.onetimeproducts.purchaseOptions.offers.list",
            {"productId": "-", "purchaseOptionId": "specific"},
        ),
        (
            "monetization.subscriptions.basePlans.offers.list",
            {"productId": "-", "basePlanId": "specific"},
        ),
        ("purchases.voidedpurchases.list", {"includeQuantityBasedPartialRefund": 1}),
        ("purchases.voidedpurchases.list", {"type": 2}),
        ("orders.batchget", {"orderIds": []}),
        ("orders.batchget", {"orderIds": ["duplicate", "duplicate"]}),
        ("orders.batchget", {"orderIds": [str(i) for i in range(1001)]}),
        ("generatedapks.list", {"versionCode": True}),
        ("generatedapks.list", {"versionCode": 2**31}),
        ("systemapks.variants.list", {"versionCode": str(2**63)}),
        ("apprecovery.list", {"versionCode": "PRIVATE-nan"}),
    ],
)
def test_bounded_typed_requests_fail_before_auth(method, extra):
    reader, auth = service()
    with patch("pubship.http.get") as read, pytest.raises(PlayError) as caught:
        (reader.read_sensitive if method in SENSITIVE else reader.read)(
            PREFIX + method, {"packageName": APP, **extra}
        )
    auth.token.assert_not_called()
    read.assert_not_called()
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "method": PREFIX + "purchases.subscriptionsv2.get",
            "parameters": {"packageName": APP, "token": "PRIVATE"},
            "body": {"PRIVATE": "value"},
        },
        {
            "method": PREFIX + "purchases.subscriptionsv2.get",
            "parameters": {"packageName": APP, "token": {"PRIVATE": "not-string"}},
        },
        {
            "method": PREFIX + "purchases.subscriptionsv2.get",
            "parameters": {"packageName": APP, "token": "PRIVATE", "alt": "media"},
        },
        {"method": {"PRIVATE": "not-string"}, "parameters": {}},
        {"method": PREFIX + "purchases.subscriptionsv2.get", "parameters": ["PRIVATE"]},
    ],
)
def test_sensitive_mcp_validation_never_leaks_argument_values_or_resolves_auth(arguments, caplog):
    async def exercise():
        async with Client(create_server(configured())) as client:
            assert "read_sensitive_publisher" in {
                tool.name for tool in (await client.list_tools()).tools
            }
            result = await client.call_tool("read_sensitive_publisher", arguments)
            assert result.is_error
            assert "PRIVATE" not in str(result.content)

    with (
        patch("pubship.auth.Auth.token") as token,
        patch("pubship.http.get") as read,
    ):
        asyncio.run(exercise())
    token.assert_not_called()
    read.assert_not_called()
    assert "PRIVATE" not in caplog.text


def test_hosted_never_exposes_either_new_tool_even_when_env_enables_sensitive(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", APP)
    monkeypatch.setenv("GOOGLE_PLAY_SENSITIVE_READ_PACKAGES", APP)
    factory = Mock(side_effect=AssertionError("Hosted discovery cannot resolve customer auth"))

    async def exercise():
        async with Client(create_server(service_factory=factory)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "read_publisher" not in names
            assert "read_sensitive_publisher" not in names
            for tool in ("read_publisher", "read_sensitive_publisher"):
                result = await client.call_tool(
                    tool,
                    {
                        "method": PREFIX + "orders.get",
                        "parameters": {"packageName": APP, "orderId": "PRIVATE"},
                    },
                )
                assert result.is_error

    asyncio.run(exercise())
    factory.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError(
            "https://androidpublisher.googleapis.com/PRIVATE",
            403,
            "PRIVATE",
            {},
            io.BytesIO(b"PRIVATE-provider-body"),
        ),
        HTTPError(
            "https://androidpublisher.googleapis.com/PRIVATE",
            429,
            "PRIVATE",
            {},
            io.BytesIO(b"PRIVATE-provider-body"),
        ),
        URLError("PRIVATE-network-detail"),
        TimeoutError("PRIVATE-timeout"),
    ],
)
def test_sensitive_transport_errors_are_redacted_and_single_attempt(failure, caplog):
    reader, _ = service()
    with patch("urllib.request.OpenerDirector.open", side_effect=failure) as send:
        with pytest.raises(PlayError) as caught:
            reader.read_sensitive(
                PREFIX + "purchases.subscriptionsv2.get", {"packageName": APP, "token": TOKEN}
            )
    send.assert_called_once()
    assert "PRIVATE" not in str(caught.value) + caplog.text


def test_real_stdio_sensitive_read_preserves_pii_but_does_not_echo_purchase_token(tmp_path):
    launcher = tmp_path / "publisher_reads_stdio.py"
    launcher.write_text("""
import io,json
from unittest.mock import patch
from pubship.auth import Auth
from pubship.server import main
def send(self,request,timeout):
    assert request.get_method()=="GET" and request.data is None and timeout==15
    assert request.get_header("Authorization")=="Bearer synthetic-stdio-token"
    base="https://androidpublisher.googleapis.com/androidpublisher/v3/applications/com.example.app"
    if request.full_url==base+"/subscriptions?pageSize=2":
        data={"subscriptions":[],"nextPageToken":"next-page"}
    elif request.full_url==base+"/purchases/subscriptionsv2/tokens/PRIVATE-purchase-token":
        data={"externalAccountIdentifiers":{"externalAccountId":"synthetic-customer"},"futureState":"UNKNOWN_PROVIDER_STATE","lineItems":[]}
    else:
        raise AssertionError("Unexpected provider request")
    return io.BytesIO(json.dumps(data).encode())
with patch.object(Auth,"token",return_value="synthetic-stdio-token"),patch("urllib.request.OpenerDirector.open",send):
    main()
""")

    async def exercise():
        connection = StdioServerParameters(
            command=sys.executable,
            args=[str(launcher)],
            env={
                "GOOGLE_PLAY_PACKAGES": APP,
                "GOOGLE_PLAY_SENSITIVE_READ_PACKAGES": APP,
                "GOOGLE_PLAY_WRITE_PACKAGES": "",
                "GOOGLE_PLAY_EDIT_PACKAGES": "",
                "GOOGLE_PLAY_TRACK_PACKAGES": "",
                "GOOGLE_PLAY_AUTH_MODE": "adc",
            },
        )
        async with Client(connection) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ["read_publisher", "read_sensitive_publisher"]:
                assert tools[name].annotations.read_only_hint
                assert not tools[name].annotations.destructive_hint
            ordinary = await client.call_tool(
                "read_publisher",
                {
                    "method": PREFIX + "monetization.subscriptions.list",
                    "parameters": {"packageName": APP, "pageSize": 2},
                },
            )
            assert not ordinary.is_error, ordinary.content
            assert ordinary.structured_content["data"] == {
                "subscriptions": [],
                "nextPageToken": "next-page",
            }
            sensitive = await client.call_tool(
                "read_sensitive_publisher",
                {
                    "method": PREFIX + "purchases.subscriptionsv2.get",
                    "parameters": {"packageName": APP, "token": TOKEN},
                },
            )
            assert not sensitive.is_error, sensitive.content
            assert (
                sensitive.structured_content["data"]["externalAccountIdentifiers"][
                    "externalAccountId"
                ]
                == "synthetic-customer"
            )
            assert "PRIVATE" not in str(sensitive.content)

    asyncio.run(exercise())


def test_independent_route_table_covers_exact_agreed_inventory():
    from pubship.coverage import PUBLISHER_READ_METHODS, SENSITIVE_PUBLISHER_READ_METHODS

    assert len(CASES) + len(SPECIAL) == 26
    assert {PREFIX + method for method in SENSITIVE} == SENSITIVE_PUBLISHER_READ_METHODS
    assert {
        PREFIX + method for method in [*CASES, *SPECIAL] if method not in SENSITIVE
    } == PUBLISHER_READ_METHODS


@pytest.mark.parametrize("token", ["", "next+/=&?é", "query&packageName=com.other.app"])
def test_pagination_is_one_opaque_query_value_and_never_changes_app(token):
    reader, _ = service()
    data = {"nextPageToken": token, "subscriptions": []}
    with patch(
        "urllib.request.OpenerDirector.open", return_value=io.BytesIO(json.dumps(data).encode())
    ) as send:
        result = reader.read(
            PREFIX + "monetization.subscriptions.list", {"packageName": APP, "pageToken": token}
        )
    url = urlsplit(send.call_args.args[0].full_url)
    assert url.path == BASE + "/subscriptions"
    assert parse_qsl(url.query, keep_blank_values=True) == [("pageToken", token)]
    assert result["data"] == data
    send.assert_called_once()


@pytest.mark.parametrize("token", ["x" * 4097, "PRIVATE\r\n", "\ud800"])
def test_pagination_bounds_reject_before_auth(token):
    reader, auth = service()
    with patch("pubship.http.get") as read, pytest.raises(PlayError):
        reader.read(
            PREFIX + "monetization.subscriptions.list", {"packageName": APP, "pageToken": token}
        )
    auth.token.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        f"applications/{APP}/externalTransactions/../PRIVATE",
        f"applications/{APP}/externalTransactions/PRIVATE%2Fother",
        f"applications/{APP}/externalTransactions/PRIVATE\ud800",
        f"applications/{APP}/externalTransactions/..",
        f"applications/{APP}/externalTransactions/PRIVATE/extra",
        "applications/com.other.app/externalTransactions/PRIVATE",
    ],
)
def test_external_transaction_embedded_resource_cannot_escape_scope(name):
    reader, auth = service()
    with patch("pubship.http.get") as read, pytest.raises(PlayError) as caught:
        reader.read_sensitive(
            PREFIX + "externaltransactions.getexternaltransaction", {"name": name}
        )
    auth.token.assert_not_called()
    read.assert_not_called()
    assert "PRIVATE" not in str(caught.value)


def test_sensitive_optin_does_not_grant_other_read_allowed_apps():
    auth = Mock()
    reader = PublisherReads(
        Settings(packages=frozenset({APP, STORE}), sensitive_read_packages=frozenset({APP})),
        auth,
        local=True,
    )
    with patch("pubship.http.get") as read, pytest.raises(PlayError):
        reader.read_sensitive(PREFIX + "orders.get", {"packageName": STORE, "orderId": "PRIVATE"})
    auth.token.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize(
    "method",
    [
        "generatedapks.download",
        "systemapks.variants.download",
        "users.list",
        "orders.refund",
        "edits.commit",
    ],
)
def test_media_developer_scope_and_write_methods_never_reach_provider(method):
    reader, auth = service()
    with patch("pubship.http.get") as read:
        for invoke in [reader.read, reader.read_sensitive]:
            with pytest.raises(PlayError):
                invoke(PREFIX + method, {"packageName": APP})
    auth.token.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize(
    "method,field,path",
    [
        ("monetization.subscriptions.get", "productId", "/subscriptions/"),
        ("purchases.subscriptionsv2.get", "token", "/purchases/subscriptionsv2/tokens/"),
        ("externaltransactions.getexternaltransaction", "name", "/externalTransactions/"),
    ],
)
def test_opaque_identifier_punctuation_is_encoded_without_query_or_fragment_escape(
    method, field, path
):
    reader, _ = service()
    opaque = "opaque?alt=media#fragment=+é"
    params = {"packageName": APP, field: opaque}
    if field == "name":
        params = {"name": f"applications/{APP}/externalTransactions/{opaque}"}
    with patch("urllib.request.OpenerDirector.open", return_value=io.BytesIO(b"{}")) as send:
        result = (reader.read_sensitive if method in SENSITIVE else reader.read)(
            PREFIX + method, params
        )
    url = urlsplit(send.call_args.args[0].full_url)
    assert url.path == BASE + path + "opaque%3Falt%3Dmedia%23fragment%3D%2B%C3%A9"
    assert not url.query and not url.fragment
    assert result["data"] == {}
    send.assert_called_once()


@pytest.mark.parametrize("data", [None, [], "PRIVATE-provider", 42])
def test_nonobject_sensitive_response_is_rejected_without_leaking_provider_data(data, caplog):
    reader, _ = service()
    with patch(
        "urllib.request.OpenerDirector.open", return_value=io.BytesIO(json.dumps(data).encode())
    ) as send:
        with pytest.raises(PlayError) as caught:
            reader.read_sensitive(
                PREFIX + "orders.get", {"packageName": APP, "orderId": "PRIVATE-order"}
            )
    send.assert_called_once()
    assert "PRIVATE" not in str(caught.value) + caplog.text


def test_oversized_integer_parameter_fails_redacted_before_credentials():
    reader, auth = service()
    with patch("pubship.http.get") as read, pytest.raises(PlayError) as caught:
        reader.read(PREFIX + "generatedapks.list", {"packageName": APP, "versionCode": 10**5000})
    auth.token.assert_not_called()
    read.assert_not_called()
    assert len(str(caught.value)) < 300


@pytest.mark.parametrize(
    "extra,allowed",
    [
        ({"startTime": "2", "endTime": "1"}, False),
        ({"startTime": "2", "endTime": "1", "token": ""}, False),
        ({"startTime": "2", "endTime": "1", "token": "next+/=&"}, True),
    ],
)
def test_voided_time_filters_never_enable_disabled_method(extra, allowed):
    reader, auth = service()
    with patch("pubship.http.get") as read, pytest.raises(PlayError):
        reader.read_sensitive(
            PREFIX + "purchases.voidedpurchases.list", {"packageName": APP, **extra}
        )
    auth.token.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize(
    "method,params",
    [
        ("apprecovery.list", {"packageName": APP}),
        ("generatedapks.list", {"packageName": APP, "versionCode": 1.0}),
        ("purchases.voidedpurchases.list", {"packageName": APP, "startIndex": 1.0}),
    ],
)
def test_required_semantics_and_integer_types_stricter_than_discovery_fail_before_auth(
    method, params
):
    reader, auth = service()
    with patch("pubship.http.get") as read, pytest.raises(PlayError):
        (reader.read_sensitive if method in SENSITIVE else reader.read)(PREFIX + method, params)
    auth.token.assert_not_called()
    read.assert_not_called()
