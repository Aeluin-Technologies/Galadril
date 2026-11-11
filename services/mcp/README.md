# Galadril MCP

The public MCP at https://mcp.gravitalia.com/ serves Galadril tools through the
existing Envoy and Cloudflare Tunnel. The service also accepts legacy resource
URLs and generation grants scoped to native Pengolo tools.

## ChatGPT plugin package

The portable archive contains both manifests at its root, the Galadril icon
and five skills, including the adapted LibreJustice procedures and references.
Build it from the repository root.

```sh
uv run --package galadril-mcp-service python -m galadril_mcp_service.package_plugin /tmp/galadril-chatgpt.zip
```

To test the MCP tools in ChatGPT Web, use **Plugins → Add custom MCP server**
with `https://mcp.gravitalia.com/` and OAuth, then install the resulting
connection and select it in a new Work chat. Register the exact callback
displayed for that connection under the Galadril OAuth client. The callback
can differ between connections. See the
[OpenAI Web quickstart](https://developers.openai.com/plugins/quickstart).

To test the skills with the custom MCP connection you already registered,
copy the `plugin_asdk_app_…` ID from the connection's ChatGPT Plugins URL and
build a workspace-specific archive:

```sh
uv run --package galadril-mcp-service python -m galadril_mcp_service.package_plugin \
  /tmp/galadril-chatgpt-linked.zip --chatgpt-app-id "$GALADRIL_CHATGPT_APP_ID"
```

Set `GALADRIL_CHATGPT_APP_ID` to the ID copied above. The native manifest
`.codex-plugin/plugin.json` declares `./.app.json` at its root. This archive
uses the already registered connection, with its existing OAuth configuration.
It omits bundled MCP transport declarations, which can cause ChatGPT to
classify imported plugins as Desktop only even when the URL is remote.
Use an account and workspace with access to that app. Verify that the
imported plugin lists the required app and that a new web chat can execute
a tool from a bundled skill.
See [OpenAI's existing-app package format](https://learn.chatgpt.com/docs/enterprise/plugin-management#reference-an-existing-app-with-appjson).

For public distribution, build without the workspace-specific ID, upload
the portable ZIP using **With MCP** in the [submission portal](https://developers.openai.com/plugins/deploy/submission),
then select **MCPs → Connect**, authorize the scanner account, and verify that
the discovered tools appear in the portal. The ZIP supplies the OAuth client
settings. The portal completes the connection, and users authorize their own
accounts through ChatGPT. Complete review and
publication before relying on the listing for web availability.

The server supports MCP `2026-07-28` through `server/discover` and
`2025-11-25` through `initialize`. Modern clients discover the Skills extension,
then use `skills/list` and `skills/get` to get the manifest and SHA-256 for each
file. Resource-aware clients can also use `resources/list` and `resources/read`
at `skill://` URIs. Hosts with plugin skills can use the bundled files directly.
MCP discovery and tool calls require a bearer token. The HTTP OAuth challenge
starts the initial connection. If a verified caller lacks a tool scope, the
tool returns an OAuth challenge in `mcp/www_authenticate` so ChatGPT can
request additional consent.

## Workspace tools

`https://mcp.gravitalia.com/` exposes legal search, mailbox reads, draft preparation,
email review, authorized data search, template discovery and management, LaTeX
compilation and job publication. Tool arguments have bounded schemas. Gateway
derives the actor and tenant from verified identity, then checks current IAM for
every operation. An OAuth scope is an additional restriction, never an IAM grant.

| Scope | Operations |
| --- | --- |
| `workspace:use` | Required for this MCP resource |
| `legal:read` | Search public law and verify citations against Légifrance |
| `email:read` | Accessible accounts, contacts, messages, threads and attachments |
| `email:write` | Prepare drafts and request review of an exact revision |
| `documents:read` | Read permitted templates; attach an authorized stored file |
| `documents:write` | Manage templates, compile and publish artifacts |
| `data:read` | Permission-filtered indexed data search |

Configure an operator-owned OAuth client named
`galadril-mcp-chatgpt` in `AUTH_MCP_CLIENTS`, using the exact callback shown by
ChatGPT, the resource URL including its trailing slash, and `workspace:use` plus
the desired tool scopes. Enter the client ID manually and use PKCE S256 with
token endpoint authentication method `none`. Existing connections can keep
using `/law/mcp` or `/workspace/mcp` with their original clients and scopes.

### Sending email

1. Read accessible accounts and, for replies, the original message/thread.
2. Save the draft. Existing drafts require their current revision. Generated PDF
   attachments can be supplied by `file_ids`, with `documents:read`: Gateway
   transfers their checked bytes directly to the encrypted draft store. Results
   contain attachment metadata, never the PDF bytes or private compiler bindings.
3. Show the sender, recipients, message and attachments for approval in the
   connected app. Call `send_email` with the exact approved revision.
4. If **Require email approval in Galadril** is enabled in Account, the tool
   returns `approval_required` and a review link. The user confirms that
   revision in their Galadril session.

Gateway trusts OAuth MCP clients to obtain approval in their own interface
when the account setting is off. The mail service checks mailbox permissions
and the exact draft revision before delivery. Editing a draft invalidates the
prior revision, and repeating a completed send returns its status.

### Compiling documents

Discover a template and read its entrypoint, CLS and included TeX before writing.
Use the same `project_id` throughout discovery, compilation and job polling. Use
a stable UUID `idempotency_key` for each compilation. For a vault-bound template,
check readiness with `document_case_status` and pass the requested body in
`document.source`. Poll `document_job_status`, then save with `document_job`.

External compiler executions are bound to tenant, user, OAuth client and project;
they do not create artificial Pengolo generations. Native generations reuse the
same compiler authorization and publication logic. Private profile fields remain
in the encrypted vault and are bound only through the trusted compiler callback.
Published files retain the existing IAM, checksum checks and retention policy.

Publication returns both `resource_link` and a textual HTTPS download URL. The
URL requires a Galadril browser session and current file access. Raw object-store
keys, credentials and PDF bytes are not included in model results.

## Pengolo transport

The shipped Compose configuration sets `SCRIBE_WORKSPACE_MCP_URL`. Workspace
operations then go through this MCP, including mail, templates, compilation,
retrieval and public Web search. Local UI adapters retain question cards and
progress events. Execution checkpoints and conversation memory remain on their
internal control channel. An MCP error never triggers a second native execution
of a potentially mutating operation.

Gateway exchanges the generation capability for a short-lived resource-bound
grant. The parent capability stays inside Gateway. The internal adapter is hidden
from the public tool catalogue. Gateway rechecks the generation, project and
specialized-context restrictions before forwarding each native operation.

## Federated legal search

The default legal search queries Légifrance and LibreJustice concurrently. It
returns up to five hits in each provider’s ranking and skips cross-provider
duplicates identified by official document ID. LibreJustice uses its reranker
for decisions and the requested version date for dated normative searches.
The development CLI action `mcp-keys` can add the LibreJustice and PISTE keys
individually. `up` mounts them as read-only files. Other deployments can set
`LIBREJUSTICE_API_KEY_FILE` to a mounted private file or supply
`LIBREJUSTICE_API_KEY` through the service environment. Send only public legal
search terms to these services.

Use `search_decisions` for reproducible LibreJustice passes. Its `request`
supports `solution`, `seat`, `formation`, `publication`, `legal_domain`,
`legal_instrument`, `legal_article`, `search_in`, date bounds, mode and sort.
The response retains the upstream facets, pinned hits and reported total.
`get_decision` reads the complete response, including the appeal chronology
when present. `search_norms` and `get_norm` accept `as_of` and preserve the
provider's source and version information. Use `fetch` and `verify_citation`
to check French decisions against Légifrance and use the dated official funds
to establish the applicable French norm. Consult the official publisher for
other legal orders. Oversized responses require another authorized source for
the complete document.

Each provider can be unavailable independently. The citation verifier always
re-fetches from Légifrance and never uses LibreJustice or Judilibre as proof.

### Optional PISTE credentials

`/law/mcp` remains registered even without PISTE credentials so previously
connected clients can recover gracefully. Missing, invalid or unavailable
credentials produce a normal `status: unavailable` tool result, with no verified
source or citation. Other tools and agent iterations remain available.

The standard MCP service has no PISTE secret mounts. The development bootstrap
mounts each available `.local/piste-key` or `.local/piste-oauth-key` independently,
without copying its contents. The optional `infrastructure/docker/law-mcp.yaml`
overlay supports deployments where both files are supplied explicitly.
Credentials are loaded lazily, outside discovery. After an initially missing
configuration becomes available, a subsequent call can activate the provider.
PISTE errors never include credential details in the tool result.

## Validation

The following command runs the fixture scenario and regression suites.

```sh
bazel test //services/mcp/tests:workspace_test //plugins/tests:law_mcp_test \
  //plugins/tests:mcp_auth_test //plugins/tests:document_templates_test \
  //plugins/tests:piste_test //plugins/tests:law_instruction_test
```

The scenario starts with opposing `solution` filters, reads a decision and a
historical norm, verifies the citation against an official-source fixture,
then reads the `recoursadmin` template target, submits LaTeX, polls the
compilation and receives a session-protected PDF link. LibreJustice, PISTE and
Gateway are fixture boundaries. The scenario exercises the actual MCP tools
and wire contracts.

## Protocol integration boundaries

The workflow package describes tool usage for both ChatGPT and Galadril clients.
The same Markdown sources and references are served as MCP resources. Skill
installation and MCP resource discovery do not grant access to workspace data.

| Convention | Integration and verification |
| --- | --- |
| SEP-414 | The native MCP client places W3C trace context in request metadata. Workspace operations continue that trace into Gateway over HTTP. Baggage stays at the incoming boundary. |
| SEP-986, SEP-1303, SEP-1613 | Keep existing tool names and typed request schemas stable. Invalid arguments must return a tool error before Gateway is invoked. |
| SEP-1330, SEP-1036 | The authorized Galadril review link remains the out-of-band confirmation path for email accounts with review enabled. Protocol elicitation remains pending. |
| SEP-1024 | Applies to installing locally executable MCP servers on the client. This plugin connects to a remote HTTPS endpoint. |
| SEP-1577 | Galadril research uses its own model runtime. |
| SEP-1686, SEP-2663 | Compilation has durable job handles through `compile_document` and `document_job`. The standard Tasks extension and durable legal research through the external MCP catalogue remain pending. |
| SEP-1865 | `document_job_status` has a read-only, network-free MCP App. The same tool returns text and structured data to clients without App support. |
| SEP-2640 | The modern Skills catalogue lists the shipped files with digests. |
| MCP Events | Subscriptions, webhook delivery and scheduled legal monitoring remain pending. |

The `mcp.librejustice.request` trace records provider operation, HTTP status
and outcome. The `galadril.mcp.librejustice.duration` OTLP histogram records
seconds by operation and outcome. Queries, source addresses and credentials
stay out of those dimensions.
