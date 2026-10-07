"""Local-only MCP registration helpers for independently configured account access."""

from typing import Any

from mcp.server.mcpserver.tools import Tool
from mcp_types import ToolAnnotations

from .account_access import AccountAccess
from .account_access_config import READ_METHOD, WRITE_METHODS, AccountAccessSettings


def build_account_tools(settings, auth, safe_tool):
    config = (
        settings
        if isinstance(settings, AccountAccessSettings)
        else AccountAccessSettings.from_env(settings)
    )
    if not config.developers or not config.methods:
        return []
    service = AccountAccess(config, auth, local=True)

    @safe_tool
    def read_account_access(method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Read one users.list page for an explicitly granted developer account.
        Local only. Returns personal email addresses, permissions and access states to
        the MCP client. Preserve pagination tokens and partial flags; never infer that
        invitations are accepted or that listed permissions have finished propagating.
        """
        return service.read(method, parameters)

    @safe_tool
    def prepare_account_access(
        method: str,
        parameters: dict[str, Any],
        body: dict[str, Any] | None = None,
        acknowledge_access_effects: bool = False,
    ) -> dict[str, Any]:
        """Review an explicitly scoped local users/grants mutation before execution.
        Requires exact developer, method, target user/app and permission ceiling grants.
        Reads all account pages, refuses partial targets, and retains the original token.
        No mutation occurs here. Review the full proposed request and account effects.
        """
        return service.prepare(method, parameters, body, acknowledge_access_effects)

    @safe_tool
    def apply_account_access(operation_id: str, confirmation: str) -> dict[str, Any]:
        """Execute one reviewed account access change with the original credential.
        Repeat operation_id exactly; rechecks grants and complete paginated baseline.
        Single attempt, no refresh, retry or rollback. Access may take up to 48 hours
        to propagate. Confirmation is not proof of human approval.
        """
        return service.apply(operation_id, confirmation)

    functions = [read_account_access] if READ_METHOD in config.methods else []
    if config.methods & WRITE_METHODS and READ_METHOD in config.methods:
        functions += [prepare_account_access, apply_account_access]
    result = []
    for fn in functions:
        tool = Tool.from_function(
            fn,
            structured_output=True,
            annotations=ToolAnnotations(
                read_only_hint=fn is not apply_account_access,
                destructive_hint=fn is apply_account_access,
                idempotent_hint=fn is read_account_access,
                open_world_hint=True,
            ),
        )
        arguments = tool.fn_metadata.arg_model
        arguments.model_config.update(extra="forbid", strict=True, hide_input_in_errors=True)
        arguments.model_rebuild(force=True)
        tool.parameters = arguments.model_json_schema(by_alias=True)
        result.append(tool)
    return result
