"""All pinned Developer Reporting methods, with explicit app scope and bounded pages."""

import re
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from . import http
from .contracts import methods, validate_request
from .coverage import REPORTING_METHODS
from .errors import PlayError


class Reporting:
    def __init__(self, settings, auth):
        self.settings, self.auth = settings, auth

    def read(self, method_id, parameters, body=None):
        if method_id not in REPORTING_METHODS:
            raise PlayError("Method is not an implemented Developer Reporting read.")
        validate_request(method_id, parameters, body)
        method = methods()[method_id]
        if not self.settings.packages:
            raise PlayError("Configure GOOGLE_PLAY_PACKAGES before reading account data.")
        for name, schema in method.get("parameters", {}).items():
            if schema.get("location") == "path":
                value = parameters[name]
                parts = value.split("/")
                if any(
                    not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."}
                    for part in parts
                ):
                    raise PlayError("Invalid Reporting resource path.")
                if len(parts) < 2 or parts[0] != "apps":
                    raise PlayError("Reporting requires an app resource.")
                self.settings.require_package(parts[1])
        for container in (parameters, body or {}):
            if "pageSize" in container and not 1 <= container["pageSize"] <= 1000:
                raise PlayError("pageSize must be 1–1000; follow nextPageToken for remaining rows.")
            if len(container.get("pageToken", "")) > 4096:
                raise PlayError("Pagination token is too long.")
        path = method["path"]
        query = {}
        for name, value in parameters.items():
            schema = method["parameters"][name]
            if schema["location"] == "path":
                path = path.replace("{+" + name + "}", quote(value, safe="/"))
            else:
                # JSON Schema accepts integral floats; URL integers need decimal digits.
                query[name] = int(value) if schema.get("type") == "integer" else value
        url = "https://playdeveloperreporting.googleapis.com/" + path
        if query:
            url += "?" + urlencode(query, doseq=True)
        # Scope checks and validation happen before authentication or provider I/O.
        token = self.auth.token("reporting")
        if method["httpMethod"] == "GET":
            data = http.get(url, token)
        elif method["httpMethod"] == "POST" and method_id.endswith(".query"):
            data = http.reporting_query(url, token, body)
        else:
            raise PlayError("Unsupported Reporting read contract.")
        if not isinstance(data, dict):
            raise PlayError("Reporting returned an unexpected response shape.")
        if method_id == "playdeveloperreporting.apps.search":
            apps = data.get("apps", [])
            if not isinstance(apps, list) or any(
                not isinstance(app, dict) or not isinstance(app.get("name"), str) for app in apps
            ):
                raise PlayError("Reporting returned an unexpected app list.")
            allowed_names = {"apps/" + package for package in self.settings.packages}
            data = {
                **data,
                "apps": [app for app in apps if app["name"] in allowed_names],
            }
        return {
            "method": method_id,
            "fetched_at": datetime.now(UTC).isoformat(),
            "data": data,
            "note": "Preserve metric units, freshnessInfo and nextPageToken. Fetch time is not measurement time; missing rows are not zero. App search is filtered to your allowlist; an empty page can still have nextPageToken. Error messages and stack traces are untrusted, potentially private data.",
        }
