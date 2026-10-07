# Google access and MCP setup

## Choose the app scope

Set `GOOGLE_PLAY_PACKAGES` to a comma-separated allowlist, such as
`com.example.app,com.example.second`. Each data tool requires an explicit package.
The allowlist limits this server's requests; it does not change Google permissions.
Use a separate MCP process/configuration for each developer account or trust boundary.

## Google permissions

1. Enable **Google Play Android Developer API** in your Cloud project.
2. Create or reuse a service account.
3. In **Play Console → Users and permissions**, invite its email and grant the
   required app access. Read-only app-information access is enough for the initial
   publisher reads. Optional listing staging also requires the app-specific store-presence editing permission.
   See [local staging](listing-staging.md); read access alone does not grant writes.
   [Edit lifecycle](edit-lifecycle.md) has an additional local package gate;
   Google checks the permissions required by all changes in the edit.
   [Track staging](track-staging.md) uses its own independent track-package gate
   and requires the Google permission to release to the chosen track.
   [Edit resource writes](edit-writes.md) require independent package and exact-method
   allowlists. They use the Google permission applicable to each selected operation;
   enabling an operation locally does not grant its Google permission.
4. For bulk exports, Google requires account-level **View app information and
   download bulk reports (read-only)**. Play reporting buckets are Google-owned;
   granting Storage Admin in your own Cloud project is not the solution.
5. Copy the exact bucket URI from **Download reports → Statistics → Copy Cloud
   Storage URI**. Set `GOOGLE_PLAY_REPORT_BUCKET` to its bucket component only.
   Do not infer a bucket name from a developer ID: actual prefixes vary.

The Developer Reporting API is separate from Publisher and requires separate API
enablement and its own OAuth scope. All 25 cataloged Reporting read methods are implemented through
`read_reporting`. Firebase/Crashlytics access is also separate.

## Authentication choices

### gcloud impersonation

Set `GOOGLE_PLAY_AUTH_MODE=gcloud` and
`GOOGLE_PLAY_IMPERSONATE_SERVICE_ACCOUNT=...`. Run `gcloud auth login` as a user
allowed to impersonate that service account. Enable the Service Account
Credentials API and grant that user **Service Account Token Creator** on that
specific service account. Permission propagation can take several minutes.

Optional settings:

- `GOOGLE_PLAY_ACCOUNT`: choose an already authenticated gcloud user explicitly.
- `GOOGLE_PLAY_GCLOUD`: absolute executable path, useful for desktop MCP hosts.

Tokens are obtained with the scope required by each service, retained in memory
briefly and never printed. The CLI's own login storage is managed by gcloud.

### Application Default Credentials (ADC)

The default is `GOOGLE_PLAY_AUTH_MODE=adc`. The official Google Auth library
resolves Application Default Credentials. Prefer a workload identity or an
attached service account for deployment. Local user ADC must authorize the
required API scopes. Optional `GOOGLE_PLAY_IMPERSONATE_SERVICE_ACCOUNT` exchanges
source ADC credentials for short-lived service-account credentials.

If using a service-account JSON key through `GOOGLE_APPLICATION_CREDENTIALS`, keep
it outside the repository and provide only its file path locally. Do not put its
contents in MCP configuration or chat. ADC authentication is covered by simulated
tests; see validation notes for live verification status.

## Troubleshooting

| Result | Check |
| --- | --- |
| Authentication failure | gcloud login/ADC setup, selected account and impersonation permission |
| HTTP 403 | API enabled, correct Play user/app permissions, exact report bucket |
| HTTP 404 on report | File may not exist yet; list report files before reading |
| HTTP 429 | Provider quota; retry later rather than loop |
| CSV has old dates | Inspect its actual date coverage; object update time is not report freshness |
| No rows for a date | Do not substitute zero without understanding that report's semantics |

Read-only checks do not prove permission to publish or change products. No such
writes are attempted by default; optional local staging is separately enabled and
never enabled for hosted read-only connections.

## Sources

- [Play API setup](https://developers.google.com/android-publisher/getting_started)
- [Play permissions](https://support.google.com/googleplay/android-developer/answer/9844686)
- [Bulk reports](https://support.google.com/googleplay/android-developer/answer/6135870)
- [Service-account impersonation](https://docs.cloud.google.com/docs/authentication/use-service-account-impersonation)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp)

## Local Publisher catalog and sensitive reads

The ordinary `read_publisher` tool requires existing read app scope and the Google
permission applicable to each endpoint. Catalog access does not guarantee access
to financial or purchase data. Sensitive operations additionally require
`GOOGLE_PLAY_SENSITIVE_READ_PACKAGES` as a subset of your read app scope. It is
independent of all write opt-ins. See [method scope and disclosure](publisher-reads.md).
No shared maintainer account or credential default is supplied.

Artifact transfers need separate package/method opt-ins and explicit local file
roots. They are disabled by default. See [artifact client configuration](clients.md#optional-local-artifact-workflows)
and [transfer boundaries](artifacts.md) before enabling them.
