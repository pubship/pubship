# Tool definition quality

PubShip evaluates the actual MCP `tools/list` response against [TDQS 1.2](https://tdqs.dev/spec), published September 1, 2026. This checks what an assistant receives, rather than Python docstrings in isolation. Definition quality is separate from runtime correctness, authorization and provider acceptance.

## Reproducible offline checks

```sh
env -u GOOGLE_APPLICATION_CREDENTIALS uv run --frozen --all-extras --group tdqs python scripts/export_tool_definitions.py --output-dir /tmp/pubship-tdqs --lint
env -u GOOGLE_APPLICATION_CREDENTIALS uv run --frozen --all-extras --group tdqs python scripts/check_tool_definitions.py --reports-dir /tmp/pubship-tdqs
```

The exporter uses synthetic configurations, a temporary vault and the real MCP SDK. It blocks credential token acquisition and network connections. It captures three surfaces:

| Profile | Tools | Described input properties |
| --- | ---: | ---: |
| Local defaults | 13 | 41 of 41 |
| All local opt-ins | 41 | 122 of 122 |
| Self-host | 46 | 130 of 130 |

The development-only `tdqs` group pins the official Python CLI to 0.1.0 through `uv.lock`. It is not a PubShip runtime dependency. Its deterministic `lint` command needs no model key. Raw upstream JSON is preserved, including heuristic warnings. CI rejects missing parameter descriptions, flags, new findings and unreviewed structural candidates.

### Reviewed structural candidates

The upstream lint heuristic compares invocation cost without evaluating task equivalence. These exact candidates remain visible in its output:

| Profile | Tool | Cheaper sibling | Costs | Disposition |
| --- | --- | --- | --- | --- |
| Local defaults | `read_report` | `get_review` | 6 / 2 | Monthly CSV data and a single user review answer different questions. |
| All local opt-ins | `read_report` | `apply_account_access` | 6 / 2 | Reading a CSV and applying an authorized account change are unrelated operations. |
| Self-host | `begin_upload_transfer` | `describe_api_method` | 5 / 1 | Reserving an upload and inspecting a pinned API schema have different effects and outputs. |
| Self-host | `read_report` | `apply_account_access` | 6 / 2 | Reading a CSV and applying an authorized account change are unrelated operations. |

These dispositions are narrowly keyed by profile, tool pair and cost. They do not suppress upstream output or turn lint into a numeric TDQS score. Any changed candidate requires review.

## Contract and safety checks

Parameter descriptions explain formats, defaults, pagination, prerequisite allowlists and opaque operation identifiers. Tool descriptions distinguish local paths from self-host transfer handles, describe result semantics, and retain warnings about untrusted provider text and explicit write authorization. Self-host catalog inspection requires an authenticated connection even though it reads only pinned definitions.

Prepare tools create single-use operation records; binary preparations can also retain private file snapshots or claim self-host transfer handles. Their annotations are therefore non-read-only, non-destructive and non-idempotent. Self-host artifact download creates a temporary transfer and has the same hints. This intentionally corrects client-visible annotations and can affect a client's approval prompts. It does not change execution permissions, provider calls or confirmation gates.

Regression snapshots protect tool names, order, required fields, types, defaults, constraints, output schemas and all other annotations. Tests also preserve caller-supplied tool precedence, ensure enrichment is idempotent and confirm descriptions do not mutate shared schema objects. Existing execution, rejection, isolation and browser suites remain required.

## Scoring and publication boundaries

Passing this offline gate is not an official TDQS grade. Model-scored evaluation requires a separately configured TDQS service or model endpoint. A server-wide score alone also does not establish that every individual tool meets a threshold. Report the evaluator, specification, candidate commit and per-tool results when such evaluation is run.

A merged source improvement does not update an already published PyPI wheel or a directory's cached build. Package publication and directory refresh must be verified separately. No real Google operations are performed for this definition audit.

## Website claim verification

Checked the website source and live pages during this delivery:

| Page | Exact claim or link checked | Result |
| --- | --- | --- |
| Home | `uvx pubship`; published version `0.24.1` | Correct for the existing PyPI release, not these unreleased source changes. |
| Home | "Reads are the default. Changes require explicit configuration for the app and method, the necessary Play Console permissions, and the operation's review and execution steps." | Matches the unchanged execution gates. |
| Permissions | "Self-hosting is for your own accounts"; "pubship-server needs an exact email allowlist and keeps enrollment closed by default. There is no project endpoint." | Matches mandatory verified identity and the project boundary. |
| Permissions | "170 implemented methods from 172 inventory definitions. Tests use synthetic providers; implementation is not a claim of live Google verification." | No method inventory or provider-verification claim changed. |
| Home, Featured on | `https://glama.ai/mcp/servers/pubship/pubship` | Directory link only; does not claim a new build or refreshed score. |

No website change is needed for these definition improvements. No model-scored grade is implied by offline lint.
