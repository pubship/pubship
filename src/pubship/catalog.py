"""Complete published API inventory; planned endpoints are never executable tools."""

import json
from importlib.resources import files

from .coverage import (
    ACCOUNT_READ_METHODS,
    ACCOUNT_WRITE_METHODS,
    APPSTORE_READ_METHODS,
    APPSTORE_WRITE_METHODS,
    ARTIFACT_METHODS,
    EDIT_WRITE_METHODS,
    MONETIZATION_QUERY_METHODS,
    MONETIZATION_WRITE_METHODS,
    PUBLISHER_EDIT_METHODS,
    PUBLISHER_READ_METHODS,
    PURCHASE_LIFECYCLE_METHODS,
    REPORTING_METHODS,
    SENSITIVE_PUBLISHER_READ_METHODS,
    SHARING_METHODS,
    SIGNING_METHODS,
    SUPPORT_METHODS,
)
from .errors import PlayError

IMPLEMENTED = {
    **dict.fromkeys(SIGNING_METHODS, "apply_signing_operation"),
    **dict.fromkeys(ACCOUNT_READ_METHODS, "read_account_access"),
    **dict.fromkeys(ACCOUNT_WRITE_METHODS, "apply_account_access"),
    **dict.fromkeys(APPSTORE_READ_METHODS, "query_appstore"),
    **dict.fromkeys(APPSTORE_WRITE_METHODS, "apply_appstore_operation"),
    **dict.fromkeys(PURCHASE_LIFECYCLE_METHODS, "apply_purchase_operation"),
    **dict.fromkeys(SUPPORT_METHODS, "apply_support_operation"),
    **dict.fromkeys(SHARING_METHODS, "apply_internal_sharing_upload"),
    **dict.fromkeys(MONETIZATION_QUERY_METHODS, "query_monetization"),
    **dict.fromkeys(MONETIZATION_WRITE_METHODS, "apply_monetization_write"),
    **{
        method: "download_artifact" if method.endswith(".download") else "apply_artifact_upload"
        for method in ARTIFACT_METHODS
    },
    **dict.fromkeys(EDIT_WRITE_METHODS, "apply_edit_write"),
    **dict.fromkeys(PUBLISHER_READ_METHODS, "read_publisher"),
    **dict.fromkeys(SENSITIVE_PUBLISHER_READ_METHODS, "read_sensitive_publisher"),
    "androidpublisher.applications.tracks.releases.list": "list_releases",
    "androidpublisher.reviews.list": "list_reviews",
    "androidpublisher.reviews.get": "get_review",
    "androidpublisher.edits.tracks.update": "apply_track_update",
    "androidpublisher.edits.listings.patch": "apply_store_listing_update",
    **dict.fromkeys(
        (
            "androidpublisher.edits.insert",
            "androidpublisher.edits.validate",
            "androidpublisher.edits.delete",
            "androidpublisher.edits.commit",
        ),
        "apply_publisher_edit_operation",
    ),
    "storage.objects.list": "list_report_files",
    "storage.objects.get": "read_report",
    **dict.fromkeys(REPORTING_METHODS, "read_reporting"),
    **dict.fromkeys(PUBLISHER_EDIT_METHODS, "read_publisher_edit"),
}


def catalog():
    return json.loads(files("pubship").joinpath("methods.json").read_text())


def list_methods(api="", status="", query="", offset=0, limit=50):
    if api not in {"", "androidpublisher", "playdeveloperreporting", "storage"} or status not in {
        "",
        "implemented",
        "coming_soon",
        "provider_unsupported",
        "policy_disabled",
    }:
        raise PlayError("Unknown API or implementation status.")
    if not 0 <= offset <= 10000 or not 1 <= limit <= 200 or len(query) > 200:
        raise PlayError("Invalid catalog pagination or search; limit is 1–200.")
    data = catalog()
    rows = [
        m
        for m in data["methods"]
        if (not api or m["api"] == api)
        and (not status or m["status"] == status)
        and query.lower() in m["id"].lower()
    ]
    return {
        "sources": data["sources"],
        "total": len(rows),
        "methods": rows[offset : offset + limit],
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "scope": "All methods in the pinned Android Publisher v3 and Play Developer Reporting v1beta1 discovery documents, plus only the two Cloud Storage methods needed for bulk Play reports.",
    }
