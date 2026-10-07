# Connect an MCP client

Start with [Google access](setup.md). For local stdio, each
client launches its own process using the account configured by that user.
An unattended server should not reuse a developer's desktop login.

The local server exposes thirteen tools by default, including `describe_api_method` and
`read_reporting`. Inspect the method contract before requesting Reporting data;
the same read tool covers all 25 pinned Reporting methods. Credentials are not
needed for local catalog and contract inspection. `read_publisher_edit` additionally
inspects ten existing-edit GET operations with an explicit package and edit ID;
see [examples and limitations](publisher-edits.md).

## Claude Code

Run from your reviewed checkout, replacing paths and the example app:

```bash
claude mcp add --transport stdio --scope user google-play \
  --env GOOGLE_PLAY_PACKAGES=com.example.app \
  -- /absolute/path/to/pubship/.venv/bin/pubship
claude mcp list
```

The example uses ADC. For gcloud impersonation, add the environment variables from
[setup.md](setup.md). Do not paste access tokens or JSON key contents into the command.
Use `/mcp` inside Claude Code to check connection status. Start by asking for two
catalog entries; the catalog does not require Google credentials.

## Codex

```bash
codex mcp add google-play \
  --env GOOGLE_PLAY_PACKAGES=com.example.app \
  -- /absolute/path/to/pubship/.venv/bin/pubship
codex mcp list
```

Use absolute paths. Shell aliases and desktop application PATH values may differ.
`GOOGLE_PLAY_GCLOUD` can point to the actual gcloud executable when needed.

## Cursor plugin

The repository contains `.cursor-plugin/plugin.json` and root `mcp.json`. Configure
`GOOGLE_APPLICATION_CREDENTIALS` with the absolute path to your local service-account
JSON key file and `GOOGLE_PLAY_PACKAGES` with a comma-separated app allowlist. The
plugin starts `uvx pubship`; install uv first and keep `uvx` available to Cursor.
Never enter the JSON key contents in a plugin setting. Adding the manifest does not
mean the plugin has been accepted into Cursor's marketplace. The generic stdio
configuration in [README](../README.md#generic-mcp-harness) remains available.

## Gemini CLI

Install uv first, then run this command in a terminal after the extension manifest
is available on the repository's default branch:

```sh
gemini extensions install https://github.com/pubship/pubship
```

Enter the absolute path to your local service-account JSON key file and the
comma-separated package names when prompted. These settings declare the environment
variables passed to the MCP process; the extension starts `uvx pubship`. The settings
contain a path and package names, not the key contents. Restart Gemini CLI after
installation and verify that the client lists PubShip's tools. The credential-free
`uvx pubship --check` checks the installed version without calling Google.

See the [Gemini CLI extension reference](https://geminicli.com/docs/extensions/reference/).

## Other stdio clients

Use the executable above as the command, no arguments, and your app allowlist as
an environment variable. Do not put shell syntax in the executable field. Stdout
is reserved for MCP protocol messages; startup errors go to stderr.

## Custom Python harness

Run this with the project's installed MCP 2.3.0 dependency. Replace the executable
path and package with your own values; no Google call is made by this example.

```python
import asyncio

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main():
    server = StdioServerParameters(
        command="/absolute/path/to/pubship/.venv/bin/pubship",
        args=[],
        env={
            "GOOGLE_PLAY_PACKAGES": "com.example.app",
            "GOOGLE_PLAY_AUTH_MODE": "adc",
        },
    )
    async with Client(server) as client:
        result = await client.call_tool("list_api_methods", {"limit": 2})
        if result.is_error:
            raise RuntimeError("MCP catalog check failed")
        print(result.structured_content)


asyncio.run(main())
```

The SDK context manager initializes and closes the MCP session. Its subprocess
transport preserves a bounded set of platform environment variables and applies
the supplied overrides. Use your harness's equivalent lifecycle when integrating
another language SDK. Full desktop-client/OAuth acceptance remains separate from
this tested local protocol example.

## Optional local listing staging

Add `GOOGLE_PLAY_WRITE_PACKAGES=com.example.app` to the same client environment
only for apps you intend to modify. It must be a subset of `GOOGLE_PLAY_PACKAGES`.
Restart the MCP process to expose two additional tools: prepare and apply.
Review the returned diff before calling apply with the exact operation ID as
confirmation. See [staging behavior and recovery](listing-staging.md).
This setting never enables hosted Google writes.

For edit creation, validation, discard or commit, separately set
`GOOGLE_PLAY_EDIT_PACKAGES=com.example.app` within the write allowlist. This adds
another two tools; enabling staging alone never enables lifecycle operations.
Review [whole-edit effects and commit behavior](edit-lifecycle.md) first.

## Operator-only HTTP clients

Run the optional server yourself using [self-host setup](hosting.md). There is no project endpoint. Use your own HTTPS `/mcp` URL in a client with Streamable HTTP and OAuth support. The browser must authorize a verified email in the operator's mandatory allowlist. App and method choices independently narrow each connection.

```sh
codex mcp add pubship --url https://operator.example.test/mcp
claude mcp add --transport http pubship https://operator.example.test/mcp
```

These commands illustrate client configuration, not a verified public service or a claim that every desktop client version has passed acceptance. Start locally with `uvx pubship` after publication. For a credential-free CLI check use `pubship --check`.

Optional HTTP capabilities must be explicitly requested and consented. Read [capability contracts](hosted-capabilities.md) and [transfer-client limits](hosted-transfers.md) before requesting changes or file delivery. Client transcripts and tool results follow the client's own retention policy. Disconnect through `disconnect_google_account` and revoke Google consent when no longer needed.
