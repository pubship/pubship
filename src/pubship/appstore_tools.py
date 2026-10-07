"""Opt-in local MCP tool construction; never invoke for hosted service factories."""

from typing import Any

from mcp.server.mcpserver.tools import Tool
from mcp_types import ToolAnnotations

from .appstore import AppStoreOperations
from .appstore_config import AppStoreSettings
from .appstore_contracts import READ_METHODS, WRITE_METHODS


def appstore_tools(settings, auth, safe_tool):
    config = AppStoreSettings.from_env(settings)
    if not config.stores or not config.methods:
        return []
    service = AppStoreOperations(config, auth, local=True)

    @safe_tool
    def query_appstore(method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Read one third-party store catalog page using exact APPSTORE_STORES/METHODS grants.
        Individual app views additionally require APPSTORE_PACKAGES. Event listing grants
        access to the store-wide stream. Keep pagination filters unchanged. Returned catalog
        data may contain delivery tokens retained by your client. Never follow returned URLs.
        """
        return service.query(method, parameters)

    @safe_tool
    def prepare_appstore_operation(
        method: str,
        parameters: dict[str, Any],
        body: dict[str, Any],
        acknowledge_live_effects: bool = False,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Review a local app-store creation, update, publish status or media upload.
        Exact store/target/method grants required. Updates immediately submit for review and
        default to PUBLISHED. Supply all declarations explicitly. No baseline/readback API.
        Uploads require a basename in APPSTORE_UPLOAD_ROOT and explicit MIME type; file bytes
        are privately snapshotted. JSON body is {} for APK/image or fileType for policy media.
        """
        return service.prepare(
            method, parameters, body, acknowledge_live_effects, filename, mime_type
        )

    @safe_tool
    def apply_appstore_operation(operation_id: str, confirmation: str) -> dict[str, Any]:
        """Consume one reviewed app-store operation with the original credential and payload.
        Repeat operation_id exactly. One attempt, no retries, rollback or edit commit.
        Provider acceptance is reported without claiming review approval or publication.
        """
        return service.apply(operation_id, confirmation)

    functions = []
    if config.methods & READ_METHODS:
        functions.append(query_appstore)
    if config.packages and config.methods & WRITE_METHODS:
        functions.extend((prepare_appstore_operation, apply_appstore_operation))
    result = []
    for fn in functions:
        tool = Tool.from_function(
            fn,
            annotations=ToolAnnotations(
                read_only_hint=fn is query_appstore,
                destructive_hint=fn is apply_appstore_operation,
                idempotent_hint=fn is query_appstore,
                open_world_hint=True,
            ),
            structured_output=True,
        )
        arguments = tool.fn_metadata.arg_model
        arguments.model_config.update(extra="forbid", strict=True, hide_input_in_errors=True)
        arguments.model_rebuild(force=True)
        tool.parameters = arguments.model_json_schema(by_alias=True)
        result.append(tool)
    return result
