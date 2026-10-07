"""Strict users/grants contracts supplementing the pinned provider schema."""

import re
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from .account_access_config import (
    DEVELOPER,
    METHODS,
    READ_METHOD,
    permission_values,
    valid_app,
    valid_email,
)
from .contracts import methods, validate_request
from .errors import PlayError


def _fail():
    raise PlayError("Invalid account access target, fields or permissions.")


def target(name, grant=False, parent=False):
    if not isinstance(name, str):
        _fail()
    parts = name.split("/")
    length = 2 if parent else 6 if grant else 4
    if len(parts) != length or parts[0] != "developers" or not DEVELOPER.fullmatch(parts[1]):
        _fail()
    if length > 2 and (parts[2] != "users" or not valid_email(parts[3])):
        _fail()
    if grant and (parts[4] != "grants" or not valid_app(parts[5])):
        _fail()
    return parts[1], parts[3] if length > 2 else None, parts[5] if grant else None


def _permissions(body, field, resource):
    if field not in body:
        return
    values = body[field]
    if (
        not isinstance(values, list)
        or len(values) != len(set(values))
        or not set(values).issubset(permission_values(resource, field))
    ):
        _fail()


def request_spec(method, parameters, body=None):
    if not isinstance(method, str) or method not in METHODS:
        _fail()
    validate_request(method, parameters, body)
    parameters, body = deepcopy(parameters), deepcopy(body)
    operation = method.rsplit(".", 1)[1]
    grant = ".grants." in method
    if method == READ_METHOD:
        developer, user, app = target(parameters["parent"], parent=True)
        if "pageSize" in parameters and (
            type(parameters["pageSize"]) is not int or not 1 <= parameters["pageSize"] <= 1000
        ):
            raise PlayError("Account reads support bounded pageSize values from 1 to 1000.")
        if "pageToken" in parameters and (
            not isinstance(parameters["pageToken"], str)
            or not 1 <= len(parameters["pageToken"]) <= 8192
            or not parameters["pageToken"].isprintable()
        ):
            _fail()
        resource = None
    elif operation == "create":
        developer, user, _ = target(parameters["parent"], parent=not grant)
        resource = body.get("name")
        bd, bu, app = target(resource, grant=grant)
        if bd != developer or grant and bu != user:
            _fail()
        user = bu
    else:
        resource = parameters["name"]
        developer, user, app = target(resource, grant=grant)
    if body is not None:
        allowed = (
            {"name", "packageName", "appLevelPermissions"}
            if grant
            else {"name", "email", "expirationTime", "developerAccountPermissions"}
        )
        if not set(body).issubset(allowed) or body.get("name", resource) != resource:
            _fail()
        if grant:
            expected = "" if app.isdigit() else app
            if body.get("packageName", expected) != expected:
                _fail()
            _permissions(body, "appLevelPermissions", "Grant")
        else:
            if body.get("email", user) != user:
                _fail()
            _permissions(body, "developerAccountPermissions", "User")
            if "expirationTime" in body:
                stamp = body["expirationTime"]
                if not isinstance(stamp, str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
                    stamp,
                ):
                    _fail()
                try:
                    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    _fail()
                if parsed <= datetime.now(UTC):
                    raise PlayError("Account access expiration must be in the future.")
        if operation == "create":
            if not grant and body.get("email") != user:
                _fail()
        if operation == "patch":
            mutable = (
                {"appLevelPermissions"}
                if grant
                else {"expirationTime", "developerAccountPermissions"}
            )
            mask = parameters.get("updateMask")
            if not isinstance(mask, str):
                raise PlayError("Account patches require an explicit supported updateMask.")
            fields = mask.split(",")
            if not fields or len(set(fields)) != len(fields) or not set(fields).issubset(mutable):
                _fail()
            if not (set(body) - {"name", "email", "packageName"}).issubset(fields):
                _fail()
            # A mask with an omitted value deliberately resets that field.
    definition = methods()[method]
    path = definition["path"]
    query = {}
    for key, value in parameters.items():
        if definition["parameters"][key]["location"] == "path":
            encoded = "/".join(quote(p, safe="") for p in value.split("/"))
            path = path.replace("{+" + key + "}", encoded)
        else:
            query[key] = value
    return {
        "method": method,
        "http_method": definition["httpMethod"],
        "url": "https://androidpublisher.googleapis.com/"
        + path
        + ("?" + urlencode(query) if query else ""),
        "parameters": parameters,
        "body": body,
        "developer": developer,
        "user": user,
        "app": app,
        "resource": resource,
        "operation": operation,
        "grant": grant,
    }
