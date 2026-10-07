# Safe provider acceptance

The [method index](provider-verification.md) links 170 enabled methods to related synthetic suites. Implementation does not mean successful live execution.

## Evidence levels

1. **Inventory and implementation:** exact pinned method, route, tool and capability mapping.
2. **Synthetic contract and integration:** provider request shape, authorization boundaries, state changes, failure handling and client protocol tested without using a real customer's app or purchase.
3. **Live read:** an explicitly authorized Google read succeeds. This establishes the tested account, app, endpoint and response shape only; empty data is not evidence of all result variants.
4. **Live effect:** a separately authorized disposable-resource operation is accepted by Google and independently observed where a readback exists. Acceptance, readback and downstream publication remain distinct.

Migration QA is limited to levels 1–2. No live Google read or mutation is authorized for this migration. A failed provider permission check is not a passing positive test. An unsupported program or missing sandbox stays unavailable rather than being tested on a production customer.

## Finite current QA

- Run the established contract, transport, hosted-dispatch, real HTTP/MCP and adversarial suites on the frozen release candidate.
- Check exact target/body/query construction, capability denial, baseline drift, expiry, replay, cancellation before dispatch and uncertain outcomes after dispatch. A rejected preparation must not call a provider; an uncertain dispatched write must not be silently retried.
- Validate the generated index against the pinned inventory and ensure every supported tool family has explicit test references.
- Reuse the recorded safe live reads and run accepted-release read/refresh continuity. Do not fetch unrelated customer reviews, purchases or reports just to increase a test count.
- Record unavailable live effects accurately. No production Google mutation is required to complete this engineering evidence index.

## Optional disposable-resource validation

Before any live write, record the exact method, account/app/resource, intended effect, permission/eligibility, resource provenance, expected readback, failure interpretation, cleanup and explicit owner authorization. The account must be a legitimate tester with appropriate Play access; do not bypass provider enrollment or purchasing controls.

| Family | Required environment and evidence | Boundary |
| --- | --- | --- |
| Listing, track and edit resources | Dedicated disposable app and isolated edit; inspect the entire edit; validate and discard when possible | A successful validation is not a committed release; no production rollout for QA |
| Product/subscription catalog | Disposable product IDs in an authorized test app, with known pricing/availability and cleanup limits | Commercial eligibility, propagation and downstream customer impact are not inferred from a HTTP response |
| Purchase, refund and external transactions | Provider-supported test purchase/account and explicit financial scope | Never refund, revoke or report a real customer's purchase just to prove a method |
| Account users and grants | Dedicated disposable developer/access targets, with independently verified recovery access | Do not remove an operator's access or expand a real user's permissions for QA |
| Enterprise signing | Eligible disposable self-hosted-signing app and dedicated KMS version; provider-approved procedure | No production signing-key enrollment/rotation; irreversible effects cannot be called rollback-tested |
| App-store integration | Actual eligible third-party-store test account and disposable app resources | Ordinary Play Console ownership does not establish partner-program access |
| Artifact upload/sharing/download | Synthetic non-sensitive artifact in an authorized disposable app/edit with exact hashes | Local proxy transfer tests establish our byte path, not Google's maximum artifact acceptance |
| Sensitive reads and reporting | Explicit app/account/service consent and minimized query window/fields | No unnecessary personal-data export to public issues or CI |

When a suitable environment is unavailable, retain the synthetic evidence and the explicit live gap. Close a live-effect check only with its own receipt. For methods lacking a readback or reversal, record that limitation instead of treating an HTTP success as complete verification.
