# Install PubShip in an MCP client

Use this guide to configure local stdio access with the user's own credentials.
Read [README](README.md) and [Google access and MCP setup](docs/setup.md) for the
configuration and permission boundaries. Do not enable writes or sensitive reads
as part of this initial setup.

## 1. Install uv

Install uv on the user's computer and make `uvx` available to the MCP client.
Desktop clients can have a different PATH from a terminal. Restart the client after
changing its environment.

## 2. Prepare the user's Google access

Use a Google Cloud service account with access to the user's apps in Play Console.
Enable the Google Play Android Developer API in the user's Cloud project. In Play
Console, invite the service-account email under Users and permissions and grant
only the app permissions needed for the intended operations. Initial Publisher
reads need read-only app-information access; local configuration does not grant
Google permissions.

Save the service account's JSON key on this computer, outside this repository.
Ask the user for its absolute file path and the comma-separated package names
PubShip may access. Never ask for, paste, print or upload the key contents.

## 3. Configure the client

Add this generic stdio configuration from README to the client's MCP settings.
Replace the example path and package names with the user's supplied values:

```json
{
  "mcpServers": {
    "pubship": {
      "command": "uvx",
      "args": ["pubship"],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/absolute/private/path/service-account.json",
        "GOOGLE_PLAY_PACKAGES": "com.example.app"
      }
    }
  }
}
```

Keep this configuration on the user's computer. The package allowlist restricts
PubShip requests; it does not change Google permissions. Use separate MCP
processes or configurations for different developer accounts or trust boundaries.

## 4. Verify the installation

Run the credential-free check:

```sh
uvx pubship --check
```

Confirm it prints the PubShip name and installed version, then confirm the client
lists PubShip's tools. This checks installation and MCP connection, not Google
permissions or readiness to publish. Do not make a real Google API call as part
of this installer check.

## Troubleshooting: ADC authentication failed

The default authentication mode is ADC. Confirm the MCP client's environment has
`GOOGLE_APPLICATION_CREDENTIALS` set to the absolute path of the saved service-account
key file, not its contents. Keep that file outside the repository and restart the
client after environment changes. Review the ADC setup in [docs/setup.md](docs/setup.md)
and verify the selected service account, API enablement and Play Console app
permissions. Do not print the key or tokens while investigating. If using an
alternate authentication mode, follow its separate setup instructions rather than
mixing modes.

## Intended use

PubShip helps developers automate their own Google Play distribution work. It runs on your computer or on infrastructure you control, with your own Google credentials and Play Console permissions. The PubShip project does not run a hosted service and never receives your credentials. Use it only for apps and developer accounts that you or your organization own, and follow Google's Play Developer API Terms. Hosted PubShip instances run by others are not provided or endorsed by the PubShip project.
