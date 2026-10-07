"""Connection-specific execution evidence beside the full local API inventory."""

from .coverage import (
    APPSTORE_READ_METHODS,
    APPSTORE_WRITE_METHODS,
    ARTIFACT_METHODS,
    EDIT_WRITE_METHODS,
    EXTERNAL_TRANSACTION_METHODS,
    LEGACY_PRODUCT_WRITE_METHODS,
    MONETIZATION_QUERY_METHODS,
    MONETIZATION_WRITE_METHODS,
    ONETIME_WRITE_METHODS,
    PRICE_MIGRATION_METHODS,
    PUBLISHER_EDIT_METHODS,
    PUBLISHER_READ_METHODS,
    PURCHASE_ACTION_METHODS,
    REPORTING_METHODS,
    SENSITIVE_PUBLISHER_READ_METHODS,
    SHARING_METHODS,
    SUPPORT_METHODS,
)
from .hosted_grants import (
    ACCOUNT_DIRECTORY,
    ACCOUNT_GRANTS,
    ACCOUNT_METHOD_CAPABILITIES,
    ACCOUNT_USERS,
    ADMIN_SCOPES,
    APPSTORE_EVENTS,
    APPSTORE_SCOPES,
    READ_SCOPE,
    SIGNING_METHOD_CAPABILITIES,
    administrative_dimensions,
)

HOSTED_EDIT_RESOURCE_METHODS = EDIT_WRITE_METHODS - {
    "androidpublisher.edits.testers.patch",
    "androidpublisher.edits.testers.update",
}
HOSTED_COMMERCE_QUERY_METHODS = MONETIZATION_QUERY_METHODS
HOSTED_COMMERCE_CAPABILITIES = {
    **dict.fromkeys(
        MONETIZATION_WRITE_METHODS - ONETIME_WRITE_METHODS - LEGACY_PRODUCT_WRITE_METHODS,
        "play:subscription-catalog",
    ),
    **dict.fromkeys(ONETIME_WRITE_METHODS, "play:onetime-catalog"),
    **dict.fromkeys(LEGACY_PRODUCT_WRITE_METHODS, "play:legacy-product-catalog"),
}

HOSTED_TESTER_WRITE_METHODS = frozenset(
    {"androidpublisher.edits.testers.patch", "androidpublisher.edits.testers.update"}
)
HOSTED_SENSITIVE_READ_CAPABILITIES = {
    **dict.fromkeys(
        SENSITIVE_PUBLISHER_READ_METHODS - {"androidpublisher.edits.testers.get"},
        "play:customer-data",
    ),
    "androidpublisher.edits.testers.get": "play:tester-audience",
}
HOSTED_SUPPORT_CAPABILITIES = {
    **dict.fromkeys(
        SUPPORT_METHODS
        - {"androidpublisher.reviews.reply", "androidpublisher.applications.dataSafety"},
        "play:app-operations",
    ),
    "androidpublisher.reviews.reply": "play:review-replies",
    "androidpublisher.applications.dataSafety": "play:app-declarations",
}
HOSTED_PURCHASE_CAPABILITIES = {
    **dict.fromkeys(PURCHASE_ACTION_METHODS, "play:purchase-actions"),
    **dict.fromkeys(EXTERNAL_TRANSACTION_METHODS, "play:external-transactions"),
    **dict.fromkeys(PRICE_MIGRATION_METHODS, "play:subscriber-price-migration"),
}

HOSTED_ARTIFACT_CAPABILITIES = {
    method: "play:artifact-downloads" if method.endswith(".download") else "play:edit-artifacts"
    for method in ARTIFACT_METHODS
}
HOSTED_SHARING_CAPABILITIES = dict.fromkeys(SHARING_METHODS, "play:internal-sharing")
HOSTED_APPSTORE_CAPABILITIES = {
    method: ("play:appstore-events" if method.endswith(".list") else "play:appstore-catalog")
    for method in APPSTORE_READ_METHODS
} | {
    method: "play:appstore-media"
    if method.rsplit(".", 1)[-1].startswith("upload")
    else "play:appstore-management"
    for method in APPSTORE_WRITE_METHODS
}

REQUIRED_HOSTED_TOOLS = frozenset(
    {
        "get_connection_capabilities",
        "read_account_access",
        "prepare_account_access",
        "apply_account_access",
        "prepare_signing_operation",
        "apply_signing_operation",
        "begin_upload_transfer",
        "get_transfer_status",
        "discard_transfer",
        "prepare_artifact_upload",
        "apply_artifact_upload",
        "download_artifact",
        "prepare_internal_sharing_upload",
        "apply_internal_sharing_upload",
        "query_appstore",
        "prepare_appstore_operation",
        "apply_appstore_operation",
        "prepare_support_operation",
        "apply_support_operation",
        "prepare_purchase_operation",
        "apply_purchase_operation",
        "read_sensitive_publisher",
        "prepare_edit_write",
        "apply_edit_write",
        "query_monetization",
        "prepare_monetization_write",
        "apply_monetization_write",
        "read_publisher",
        "prepare_store_listing_update",
        "apply_store_listing_update",
        "prepare_track_update",
        "apply_track_update",
        "prepare_publisher_edit_operation",
        "apply_publisher_edit_operation",
    }
)

HOSTED_BINARY_METHODS = frozenset(
    (ARTIFACT_METHODS - {"androidpublisher.edits.apks.addexternallyhosted"})
    | SHARING_METHODS
    | {
        method
        for method, cap in HOSTED_APPSTORE_CAPABILITIES.items()
        if cap == "play:appstore-media"
    }
)

HOSTED_ACCOUNT_CAPABILITIES = ACCOUNT_METHOD_CAPABILITIES
HOSTED_SIGNING_CAPABILITIES = SIGNING_METHOD_CAPABILITIES

HOSTED_CAPABILITIES = {
    **HOSTED_ACCOUNT_CAPABILITIES,
    **HOSTED_SIGNING_CAPABILITIES,
    **HOSTED_ARTIFACT_CAPABILITIES,
    **HOSTED_SHARING_CAPABILITIES,
    **HOSTED_APPSTORE_CAPABILITIES,
    **dict.fromkeys(HOSTED_TESTER_WRITE_METHODS, "play:tester-audience"),
    **HOSTED_SENSITIVE_READ_CAPABILITIES,
    **HOSTED_SUPPORT_CAPABILITIES,
    **HOSTED_PURCHASE_CAPABILITIES,
    **dict.fromkeys(HOSTED_EDIT_RESOURCE_METHODS, "play:edit-resources"),
    **dict.fromkeys(HOSTED_COMMERCE_QUERY_METHODS, "play:commerce-query"),
    **HOSTED_COMMERCE_CAPABILITIES,
    **dict.fromkeys(PUBLISHER_EDIT_METHODS, READ_SCOPE),
    **dict.fromkeys(REPORTING_METHODS, READ_SCOPE),
    **dict.fromkeys(PUBLISHER_READ_METHODS, "play:publisher-metadata"),
    "androidpublisher.applications.tracks.releases.list": READ_SCOPE,
    "androidpublisher.reviews.list": READ_SCOPE,
    "androidpublisher.reviews.get": READ_SCOPE,
    "storage.objects.list": READ_SCOPE,
    "storage.objects.get": READ_SCOPE,
    "androidpublisher.edits.listings.patch": "play:edit-listings",
    "androidpublisher.edits.tracks.update": "play:edit-tracks",
    "androidpublisher.edits.insert": "play:edit-create",
    "androidpublisher.edits.validate": "play:edit-validate",
    "androidpublisher.edits.delete": "play:edit-discard",
    "androidpublisher.edits.commit": "play:edit-commit",
}


def execution_info(method_id, authorization):
    """Do not equate local implementation, hosted support and current consent."""
    capability = HOSTED_CAPABILITIES.get(method_id)
    if capability is None:
        return {
            "mode": "hosted",
            "implemented": False,
            "available_for_connection": False,
            "authorized_packages": [],
            "reason": "This method has no hosted execution tool in this release.",
        }
    service = (
        "reporting"
        if method_id.startswith("playdeveloperreporting.")
        else "storage"
        if method_id.startswith("storage.")
        else "publisher"
    )
    transfer_available = (
        method_id not in HOSTED_BINARY_METHODS or authorization.binary_transfers_enabled
    )
    if capability in ADMIN_SCOPES:
        dimensions = administrative_dimensions(authorization)
        if capability == ACCOUNT_DIRECTORY:
            selected = {"account_directory_developers": dimensions["account_directory_developers"]}
        elif capability == ACCOUNT_USERS:
            selected = {
                "account_user_targets": [
                    row for row in dimensions["account_user_targets"] if method_id in row["methods"]
                ]
            }
        elif capability == ACCOUNT_GRANTS:
            selected = {
                "account_grant_targets": [
                    row
                    for row in dimensions["account_grant_targets"]
                    if method_id in row["methods"]
                ]
            }
        else:
            selected = {"signing_targets": dimensions["signing_targets"].get(capability, [])}
        available = (
            bool(next(iter(selected.values())))
            and capability in authorization.scopes
            and service in authorization.google_services
        )
        return {
            "mode": "hosted",
            "implemented": True,
            "required_capability": capability,
            "required_google_service": service,
            "available_for_connection": available,
            "authorized_packages": [],
            "authorization_schema_version": authorization.authorization_schema_version,
            **{"authorized_" + key: value if available else [] for key, value in selected.items()},
            "reason": "Exact method and dimensional consent permit this operation; Google eligibility and existing/requested permission ceilings still apply."
            if available
            else "This connection lacks the exact account or signing authority and Google service required for this method.",
        }
    if capability in APPSTORE_SCOPES:
        targets = authorization.appstore_targets.get(capability, {})
        events = (
            authorization.appstore_event_stores if capability == APPSTORE_EVENTS else frozenset()
        )
        available = (
            bool(targets or events)
            and service in authorization.google_services
            and transfer_available
        )
        return {
            "mode": "hosted",
            "implemented": True,
            "required_capability": capability,
            "required_google_service": service,
            "available_for_connection": available,
            "authorized_packages": [],
            "authorization_schema_version": authorization.authorization_schema_version,
            "authorized_appstore_targets": {store: sorted(apps) for store, apps in targets.items()}
            if available
            else {},
            "authorized_appstore_event_stores": sorted(events) if available else [],
            "reason": (
                "Connection grants permit exact store/app pairs or the explicitly store-wide feed; Google eligibility and request validation still apply."
                if available
                else "The required store capability, Google service or binary transfer configuration is unavailable."
            ),
        }
    packages = sorted(authorization.capability_packages.get(capability, ()))
    available = bool(packages) and service in authorization.google_services and transfer_available
    if service == "storage" and not authorization.bucket_configured:
        available = False
    return {
        "mode": "hosted",
        "implemented": True,
        "required_capability": capability,
        "required_google_service": service,
        "available_for_connection": available,
        "authorized_packages": packages if available else [],
        "reason": (
            "Connection grants permit this tool for these packages; Google eligibility and request validation still apply."
            if available
            else "This connection lacks the required capability, Google service or report bucket. Reconnect to change consent."
        ),
    }


def annotate_method(method, authorization):
    return {**method, "execution": execution_info(method["id"], authorization)}


def annotate_catalog(result, authorization):
    return {
        **result,
        "implementation_scope": "local",
        "execution_mode": "hosted",
        "execution_note": "Implementation status describes the full local inventory. Each execution field describes this hosted connection; it does not establish provider eligibility.",
        "methods": [annotate_method(method, authorization) for method in result["methods"]],
    }
