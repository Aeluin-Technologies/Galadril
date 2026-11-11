# Galadril MCP

The public MCP at https://mcp.gravitalia.com/ serves Galadril tools through the
existing Envoy and Cloudflare Tunnel. The service also accepts legacy resource
URLs and generation grants scoped to native Pengolo tools.

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
a stable UUID `idempotency_key` for each compilation. Poll `document_job`, then
save its PDF or a batch archive.

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

Run the Bazel targets for MCP schemas, OAuth audiences, delegated operation scopes,
private attachment projections, legal-provider availability, Scribe's fallback
loop and the browser approval flow. The E2E environment loads the real central
MCP image and uses the shipped Scribe transport. Its Pengolo smoke test requires
a successful Gateway search result, rather than accepting an unavailable tool
as an empty result. Tests must not send real emails or use production PISTE keys.
