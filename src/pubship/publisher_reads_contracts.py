"""Fixed GET allowlists with bounded, credential-free request validation."""

import re
from urllib.parse import quote, urlencode

from .config import PACKAGE
from .contracts import methods, validate_request
from .coverage import PUBLISHER_READ_METHODS, SENSITIVE_PUBLISHER_READ_METHODS
from .errors import PlayError
from .play import path_segment

PREFIX = "androidpublisher."
EXTERNAL_TRANSACTION = PREFIX + "externaltransactions.getexternaltransaction"
BATCH_LIMITS = {
    PREFIX + "inappproducts.batchGet": ("sku", 100),
    PREFIX + "monetization.onetimeproducts.batchGet": ("productIds", 100),
    PREFIX + "monetization.subscriptions.batchGet": ("productIds", 100),
    PREFIX + "orders.batchget": ("orderIds", 1000),
}
OFFER_LIST_PARENTS = {
    PREFIX + "monetization.subscriptions.basePlans.offers.list": "basePlanId",
    PREFIX + "monetization.onetimeproducts.purchaseOptions.offers.list": "purchaseOptionId",
}


def _text(value, *, limit, empty=False):
    if (
        not isinstance(value, str)
        or not (0 if empty else 1) <= len(value) <= limit
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
    ):
        raise PlayError(
            "Publisher identifiers and tokens must be bounded strings without controls."
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise PlayError("Publisher identifiers and tokens must contain valid Unicode.") from None
    return value


def _identifier(value, *, limit=1024):
    _text(value, limit=limit)
    if value in {".", ".."} or any(char in value for char in "/\\%"):
        raise PlayError("Invalid Publisher identifier; supply an unencoded path segment.")
    return quote(value, safe="")


def _int64(value, *, positive=False):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"0|[1-9][0-9]{0,18}", value)
        or not (1 if positive else 0) <= int(value) <= 2**63 - 1
    ):
        raise PlayError("Publisher numeric identifiers must be bounded decimal int64 strings.")


def validate_publisher_read(method_id, parameters, *, sensitive=False):
    """Return the scoped package and fixed GET URL; never resolve credentials."""
    allowed = SENSITIVE_PUBLISHER_READ_METHODS if sensitive else PUBLISHER_READ_METHODS
    if not isinstance(method_id, str) or method_id not in allowed:
        raise PlayError("Method is not an implemented Publisher read for this tool.")
    validate_request(method_id, parameters)
    method = methods()[method_id]
    if method["httpMethod"] != "GET" or "request" in method:
        raise PlayError("Unsupported Publisher read contract.")
    if method_id == EXTERNAL_TRANSACTION:
        parts = parameters["name"].split("/")
        if len(parts) != 4 or parts[0] != "applications" or parts[2] != "externalTransactions":
            raise PlayError("External transactions require an exact application resource name.")
        package = parts[1]
        encoded_name = "applications/" + package + "/externalTransactions/" + _identifier(parts[3])
    else:
        package = parameters["packageName"]
        encoded_name = None
    path_segment(package, limit=255)
    if not PACKAGE.fullmatch(package):
        raise PlayError("Invalid Android package name.")
    if method_id in BATCH_LIMITS:
        field, limit = BATCH_LIMITS[method_id]
        values = parameters.get(field)
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= limit
            or len(set(values)) != len(values)
        ):
            raise PlayError("Batch reads require a nonempty, bounded list of distinct identifiers.")
    if method_id == PREFIX + "apprecovery.list" and "versionCode" not in parameters:
        raise PlayError("App recovery reads require an explicit versionCode.")
    if (
        method_id in OFFER_LIST_PARENTS
        and parameters["productId"] == "-"
        and parameters[OFFER_LIST_PARENTS[method_id]] != "-"
    ):
        raise PlayError("An all-products offer read requires the matching all-parent wildcard.")
    if parameters.get("expansionFileType") == "expansionFileTypeUnspecified":
        raise PlayError("Choose main or patch as the expansion file type.")
    path = method["path"]
    query = {}
    for name, value in parameters.items():
        schema = method["parameters"][name]
        if schema.get("repeated"):
            for identifier in value:
                _identifier(identifier)
        elif schema["type"] == "integer":
            if type(value) is not int:
                raise PlayError("Publisher integer parameters must be JSON integers.")
            if name in {"versionCode", "apkVersionCode"} and value <= 0:
                raise PlayError("Version codes must be positive integers.")
        elif schema["type"] == "boolean":
            if type(value) is not bool:
                raise PlayError("Publisher boolean parameters must be JSON booleans.")
        elif schema.get("format") == "int64":
            _int64(value, positive=name == "versionCode")
        elif name != "name":
            _text(
                value,
                limit=4096 if name in {"pageToken", "token"} else 1024,
                empty=schema["location"] == "query" and name in {"pageToken", "token"},
            )
        if name == "pageSize":
            limit = 100 if method_id == PREFIX + "applications.deviceTierConfigs.list" else 1000
            if not 1 <= value <= limit:
                raise PlayError("Publisher pageSize exceeds this method's supported range.")
        if name == "maxResults" and not 1 <= value <= 1000:
            raise PlayError("Publisher maxResults must be 1 through 1000.")
        if schema["location"] == "path":
            if name == "name":
                path = path.replace("{+name}", encoded_name)
            else:
                if name == "track":
                    encoded = path_segment(value, track=True)
                elif name == "editId":
                    encoded = path_segment(value, limit=1024)
                elif schema["type"] == "integer":
                    encoded = str(value)
                else:
                    encoded = _identifier(value, limit=4096 if name == "token" else 1024)
                path = path.replace("{" + name + "}", encoded)
        elif schema["location"] == "query":
            query[name] = str(value).lower() if type(value) is bool else value
        else:
            raise PlayError("Unsupported Publisher parameter location.")
    url = "https://androidpublisher.googleapis.com/" + path
    if query:
        url += "?" + urlencode(query, doseq=True)
    return package, url
