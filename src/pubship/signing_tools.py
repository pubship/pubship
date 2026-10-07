"""Opt-in signing tools, registered only by the local factory."""

from typing import Any

from mcp.server.mcpserver.tools import Tool
from mcp_types import ToolAnnotations

from .signing import Signing, SigningSettings


def signing_tools(auth, safe_tool):
    settings = SigningSettings.from_env()
    if not (settings.apps and settings.methods and settings.kms_versions):
        return []
    signing = Signing(settings, auth, local=True)

    @safe_tool
    def prepare_signing_operation(
        method: str,
        parameters: dict[str, Any],
        body: dict[str, Any],
        acknowledge_signing_effects: bool = False,
    ) -> dict[str, Any]:
        """Review enterprise self-hosted Cloud KMS enrollment or signing-key rotation.
        Requires exact SIGNING_APPS, SIGNING_METHODS and SIGNING_KMS_VERSIONS grants.
        Standard Google-managed signing requires Play Console. Supply public certificates
        as base64 PEM, never private keys. Reviews fingerprints and KMS version; does not
        mutate Google. No baseline/readback exists; Google verifies eligibility and lineage.
        """
        return signing.prepare(method, parameters, body, acknowledge_signing_effects)

    @safe_tool
    def apply_signing_operation(operation_id: str, confirmation: str) -> dict[str, Any]:
        """Send one reviewed enterprise signing request, with the original credential.
        Repeat operation_id exactly. Rechecks explicit grants and ten-minute expiry;
        consumes the operation ID before execution. No retry, rollback or readback.
        Success means provider acceptance with correlated public certificate hashes,
        not successful installation or propagation. Reconcile uncertain outcomes in Console.
        """
        return signing.apply(operation_id, confirmation)

    tools = []
    for fn in (prepare_signing_operation, apply_signing_operation):
        tool = Tool.from_function(
            fn,
            annotations=ToolAnnotations(
                read_only_hint=fn is prepare_signing_operation,
                destructive_hint=fn is apply_signing_operation,
                idempotent_hint=False,
                open_world_hint=True,
            ),
            structured_output=True,
        )
        arguments = tool.fn_metadata.arg_model
        arguments.model_config.update(extra="forbid", strict=True, hide_input_in_errors=True)
        arguments.model_rebuild(force=True)
        tool.parameters = arguments.model_json_schema(by_alias=True)
        tools.append(tool)
    return tools
