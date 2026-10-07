"""Offline request validation from pinned Google Discovery contracts."""

import json
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator

from .catalog import catalog
from .errors import PlayError


@lru_cache(maxsize=1)
def definitions():
    return json.loads(files("pubship").joinpath("contracts.json").read_text())


@lru_cache(maxsize=1)
def methods():
    return {method["id"]: method for method in catalog()["methods"]}


def json_schema(node):
    """Convert Google's schema dialect without evaluating external references."""
    if "$ref" in node:
        return {"$ref": "#/$defs/" + node["$ref"]}
    result = {
        key: node[key]
        for key in ("type", "format", "enum", "pattern", "description")
        if key in node
    }
    if result.get("type") == "any":
        result.pop("type")
    for key in ("minimum", "maximum"):
        if key in node:
            result[key] = float(node[key])
    bounds = {"int32": (-(2**31), 2**31 - 1), "uint32": (0, 2**32 - 1)}
    if node.get("type") == "integer" and node.get("format") in bounds:
        minimum, maximum = bounds[node["format"]]
        result["minimum"] = max(result.get("minimum", minimum), minimum)
        result["maximum"] = min(result.get("maximum", maximum), maximum)
    if "properties" in node:
        result["properties"] = {
            key: json_schema(value) for key, value in node["properties"].items()
        }
    if node.get("type") == "object":
        additional = node.get("additionalProperties", False)
        result["additionalProperties"] = (
            json_schema(additional) if isinstance(additional, dict) else additional
        )
    if "items" in node:
        result["items"] = json_schema(node["items"])
    if node.get("repeated"):
        result = {"type": "array", "items": result}
    return result


@lru_cache(maxsize=256)
def request_schema(method_id):
    method = methods().get(method_id)
    if not method or method["api"] == "storage":
        raise PlayError(
            "No generic request contract for this method. Use the scoped report tools for Storage."
        )
    parameters = method.get("parameters", {})
    parameter_schema = {
        "type": "object",
        "properties": {key: json_schema(value) for key, value in parameters.items()},
        "required": [key for key, value in parameters.items() if value.get("required")],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {"parameters": parameter_schema},
        "required": ["parameters"],
        "additionalProperties": False,
        "$defs": {
            key: json_schema(value)
            for key, value in definitions()[method["api"]]["schemas"].items()
        },
    }
    if "request" in method:
        schema["properties"]["body"] = json_schema(method["request"])
        schema["required"].append("body")
    return schema


def validate_request(method_id, parameters, body=None):
    value = {"parameters": parameters}
    if body is not None:
        value["body"] = body
    try:
        encoded = json.dumps(value, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise PlayError("Request must be finite JSON data.") from None
    if len(encoded) > 64 * 1024:
        raise PlayError("Request exceeds 64 KiB; narrow the query.")
    if not Draft202012Validator(request_schema(method_id)).is_valid(value):
        # ValidationError.message can include a purchase token or private request.
        raise PlayError(
            "Request does not match the pinned contract. Use describe_api_method to inspect it."
        )


def describe_method(method_id):
    method = methods().get(method_id)
    if not method:
        raise PlayError("Unknown method ID. Use list_api_methods to discover supported contracts.")
    result = {
        "method": method,
        "note": "A contract describes Google's API, not permission or implementation status.",
    }
    if method["api"] != "storage":
        schema = request_schema(method_id)
        # Send only reachable definitions, not hundreds of unrelated schemas.
        needed = {}
        pending = [schema["properties"]]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                ref = node.get("$ref", "").removeprefix("#/$defs/")
                if ref and ref not in needed:
                    needed[ref] = schema["$defs"][ref]
                    pending.append(needed[ref])
                pending.extend(v for k, v in node.items() if k != "$ref")
            elif isinstance(node, list):
                pending.extend(node)
        result["input_schema"] = {**schema, "$defs": needed}
    return result
