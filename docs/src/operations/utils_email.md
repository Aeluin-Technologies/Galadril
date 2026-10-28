# Email accounts with the official utils plugin

The `utils` email service collects multiple IMAP mailboxes, stores mail
durably, delivers opted-in messages and attachments through native ingestion, and
prepares or sends replies through verified TLS SMTP. Google and Microsoft accounts
use OAuth consent with IMAP/SMTP; other providers use passwords or app passwords.
Providers must enable these protocols. Gmail REST history and Microsoft Graph
delta synchronization are not adapters in this version.

Open **User settings → Email** (`/settings/user#email`). The former
`/settings/email` address redirects there. **OAuth** (`/settings/user#oauth`)
contains personal service connections. Choose a row in the email account list
to open its **Connection**, **Messages** or **Replies** panel. The account's
action menu groups connection tests, editing, access grants and deletion.
Each connection
has its own folders, collection interval, ingestion choice, response profile and
access grants. On first collection, the default is **new messages only**. Choose
**all available messages** to import history; review automation settings before
doing so. Collection uses read-only IMAP and does not mark messages as read.
Local category labels organize the collected copy; they do not modify provider
folders or flags. Removing a connection removes its local mail, drafts and tokens,
without deleting messages at the provider.

### Sending without IMAP

The IMAP server is optional. Leave it empty in the server-settings step for an
outbound-only connection, such as SMTP2GO. No separate mode switch is needed.
Galadril tests SMTP authentication, retains the connection in Pengolo's sender
selector, and keeps the same approval and duplicate-delivery protections.
It does not poll the connection, display receiving controls or ingest messages.

SMTP supports STARTTLS on ports 25, 80, 587, 2525 and 8025, and implicit TLS on
465, 8465 and 443. TLS and certificate verification are mandatory on every port;
credentials are never sent before the secure channel succeeds. For SMTP2GO,
`mail-eu.smtp2go.com:2525` uses STARTTLS; enter the SMTP user credentials and a
verified sender address from your SMTP2GO account, following the
[provider's SMTP relay instructions](https://developers.smtp2go.com/docs/smtp-relay).
Server probing sends no credentials or email.

### Detect servers from a domain

For a new connection, enter only the email address and select **Continue**.
The wizard discovers secure servers before asking for credentials. If discovery
is incomplete, it opens the server-settings step; detected settings can also be
edited explicitly. Editing an existing connection retains its advanced form.

The worker first queries `_imaps._tcp`, `_submissions._tcp` and `_submission._tcp`
DNS SRV records following [RFC 6186](https://www.rfc-editor.org/rfc/rfc6186.html)
and [RFC 8314](https://www.rfc-editor.org/rfc/rfc8314.html). Missing services fall
back to Mozilla's public ISPDB, the domain's HTTPS Autoconfig endpoint and
`/.well-known/autoconfig/mail/config-v1.1.xml`. One parent-domain HTTPS Autoconfig
endpoint is also checked for subdomains. For example, student addresses at
`etu.univ-lorraine.fr` can use `autoconfig.univ-lorraine.fr`.

Only the domain is used. Provider templates receive the synthetic address
`discovery@<domain>`, never the user's mailbox name. XML must explicitly match the
requested domain and declare IMAP 993 with TLS and SMTP 465 with TLS or 587 with
STARTTLS. Documents are bounded to 64 KiB; document types, redirects, unverified
certificates and plaintext HTTP fallback are rejected. HTTPS connections pin
validated public addresses and do not use environment proxies. A bad certificate
on one candidate does not prevent trying another verified candidate.

DNS answers and successful public configurations are cached with bounded sizes.
Discovery has a total deadline, bounded concurrency and record counts. Endpoints
are validated against the operator network policy, and actual mail connections
revalidate and pin addresses and verify TLS certificates. No credentials are read
or sent and no account is saved during discovery. Discovery is authenticated and
unavailable to delegated model tool calls. MX records are not substitutes for
IMAP access or SMTP submission records. JMAP is not an adapter in this version.

Gmail server detection works independently of application registration. The
wizard offers OAuth only when the operator has configured a Google client. With
no client, it goes directly to the sign-in step and requests an app password.
A regular Google account password is not the app password. Availability depends
on the account's Google security and organization policies. University templates
with no username rule ask for the institution's login identifier explicitly.
Changing an existing connection's host still requires replacement credentials.

## Resolve recipients from an address book

Pengolo can search a connected Zimbra account's personal contacts and organization
directory before preparing an email. University of Lorraine student and staff
domains have maintained HTTPS bindings. Other Zimbra deployments can configure
`UTILS_ZIMBRA_DIRECTORIES` on the email worker as a JSON map of exact email domains
to trusted Zimbra hostnames, for example `{"example.org":"mail.example.org"}`.
Only the fixed HTTPS `/service/soap` endpoint is used, with verified certificates,
public-address validation and DNS pinning. Credentials never enter the model.

Lookup requires the connection's `read` permission. It uses a bounded live
autocomplete query, including the global address list, rather than copying the
organization directory. Password-authenticated Zimbra accounts are supported;
Google/Microsoft OAuth accounts do not gain a Zimbra adapter. Up to eight readable
accounts are queried and at most twenty deduplicated individual contacts are
returned. Sender account addresses are withheld. Incomplete searches and multiple
matches require clarification through Pengolo's question card before a draft is
created. The user still chooses a sending account and approves delivery.

## Connection and action permissions

Pipeline visibility grants no access to an email account or its credentials.
The creator owns the connection and can grant independent rights to active users
in the same tenant:

| Right | Allows |
| --- | --- |
| `view` | See connection metadata and collection status |
| `read` | Read collected messages, attachments and drafts |
| `use` | Test/synchronize the connection and create or generate drafts |
| `edit` | Edit settings, response profiles and local categories; replace credentials |
| `delete` | Delete the local connection and its stored content |
| `reveal` | Retrieve the private login name and authentication method |
| `share` | Inspect connection grants and audit events |
| `approve` | Approve an exact draft revision |
| `send` | Send an approved draft revision |
| `automate` | Configure explicitly authorized automatic replies |

Any granted right also makes basic connection metadata visible. Only the owner
can change grants, including granting `reveal`; an editor cannot grant it to
themselves. An editor or deleter does not need `reveal`. Existing secrets are
absent from normal API responses, pipeline YAML, catalog metadata and dashboard
state. Passwords and provider tokens cannot be read back, even by the owner.
Replacement credentials are write-only. A password-only update preserves the
encrypted login name internally. Changing the sender or provider
endpoint requires complete replacement credentials, so old secrets cannot be
redirected to a different host. Concurrent changes use revision checks.

Generating a reply from collected mail requires both `read` and `use`; it does
not require permission to edit the connection. Disabling a connection stops
collection and prevents sending, including already approved drafts.

Login-name disclosure is deliberate and temporary in the dashboard. It is never
needed for ordinary connection management. Account owners should grant it sparingly.
The worker also rechecks active tenant membership through Gateway before
background collection and before sending. Connection ACLs are an independent
boundary; they do not bypass native tenant authorization.

## Replies and automation

Select any configured **Scribe model alias**, local or API-backed. If it becomes
unavailable, Scribe automatically tries up to three configured models supporting
the same reasoning options, preferring the same provider. If its configuration
has been removed, the configured default supplies the compatibility baseline.
With no usable configured model, generation fails without sending. Registered
Skills have searchable names and descriptions; language is searchable too.
Instructions, tone and a Markdown or HTML signature are configurable. Single
line breaks are preserved; `<small>` is supported for smaller mentions. Signatures
are sanitized and appended after generation, with a plain-text alternative.
Metadata fields are substituted after Markdown rendering, preventing them from
introducing links or images through Markdown syntax. Images require HTTPS URLs
without credentials, IP literals, local hostnames or nonstandard ports. The
service never fetches them. The isolated preview blocks image loading until
the viewer selects **Show external images**; recipients retain their mail client
image-loading policy. Scripts, frames, styles and inline data images are removed. Selecting an API model sends the email
context to that model's provider under the existing Scribe configuration.

The three modes are:

1. **Generate on demand:** collection does not call a model. Generate from a
   selected message or compose manually.
2. **Prepare drafts for approval:** collection generates categorized drafts.
   A user with `approve` reviews the exact revision; `send` is separate.
3. **Automatic replies:** requires `automate`, exact allowed sender addresses,
   and a global rolling 24-hour maximum (1–100, default 10). Optionally set a
   per-sender rolling 24-hour maximum or one acknowledgment per conversation.
   There is no fixed one-reply-per-sender rule. Conversation identity follows
   `References` / `In-Reply-To`, independently of the sender.

Every edit to a draft clears approval. Updating connection settings also clears
existing approvals. Automatic replies keep the original sender as the only
recipient and do not send attachments. Automated messages, mailing lists,
delivery reports and messages not addressed to this mailbox require review.
Permanent source deduplication prevents replies to the same message twice.
Email-supplied instructions cannot grant tools, credentials or send authority.

The optional `utils.mcp.email` Scribe toolset reads authorized mail, searches it,
classifies it and saves drafts through the existing delegated conversation
lease. It cannot reveal credentials, configure accounts, approve or send.
`utils.skill.email` supplies the corresponding packaged correspondence Skill.
Registered plugin Skills also appear as read-only specialists in Pengolo;
selection and custom-specialist management use separate views.

The native `propose_email` tool only prepares a durable, editable card containing
To/Cc/Bcc, subject and body. It receives no sender account or credentials and
cannot approve or send. Clicking Send resolves the current user's authorized
accounts in the interface. One usable account is selected automatically; multiple
accounts require an explicit choice. The worker still checks approval, send
rights and the exact draft revision. Bcc addresses are envelope recipients only.
The proposal identifier survives reloads, so reopening a sent card cannot resend it.

New response profiles start with an empty signature. An optional
`Ref. : <code>{{ ref }}</code>` field can be added explicitly. Signature fields
are substituted at sending time, never evaluated as code. The preview uses
example values. Existing custom signatures remain unchanged.

| Field | Value |
| --- | --- |
| `{{ ref }}` | Root Message-ID followed by the message number in the thread |
| `{{ subject }}` | Subject of the outgoing email |
| `{{ sender }}` | Sending address selected by the user |
| `{{ recipient }}` | To recipients; Cc and Bcc are excluded |
| `{{ date }}` | Sending date in UTC |

Incoming messages receive a tenant/account-scoped reference derived from their
durable identity. Recollection and replay preserve it. Generated replies retain
the source reference, as do manual replies whose source identity is verified.
Standalone drafts receive their own reference, preserved through edits. It is
included in the ingestion envelope metadata and the `X-Galadril-Reference`
outgoing header, allowing collection, downstream analysis and replies to be
correlated. References convey no access rights. Template values are HTML-escaped
before sanitization, and expansion is bounded to 64 KiB.

SMTP cannot guarantee exactly-once delivery across a lost acknowledgment.
Some providers accept an email address for IMAP but require a separate account
login for SMTP. Use the login supplied by the provider's autoconfiguration or
mail-client settings; an IMAP authentication success alone does not verify SMTP.
The UI reports incoming synchronization and SMTP login verification separately.
`Test connection` authenticates against SMTP even when IMAP is configured, then
checks incoming mail. A known SMTP authentication rejection remains visible
after successful IMAP synchronization. Editing connection settings resets the
SMTP observation to `not_checked`; a successful test or send marks it `verified`.
These observations do not certify delivery of any individual message.
Before contacting SMTP, the service durably claims the approved draft. A crash
or uncertain response leaves `sending` or `uncertain`, blocking blind retries.
Check provider logs or Sent mail using the displayed stable Message-ID before
deciding whether to compose another message. There is no automatic resend or
delivery reconciliation mutation in this version.

## Provision and deploy

The plugin is discovered from the reviewed package at startup. Deployment starts
its listener as a separate worker. Deploy the same reviewed bundle to Scribe, Vision
and the plugin worker, and restart those runtimes after installation.

Generate private deployment files once:

```sh
bazel run //plugins:utils_init -- /absolute/private/utils-email
```

The directory is created with mode `0700`; `service-key`, `tracking-key` and `keyring.json` use
`0600`. The command refuses to overwrite existing files and never prints keys.
Set file ownership for runtime UID/GID 1000. Prepare a separate persistent data
directory owned by that runtime. Mount credentials read-only and keep keys out
of pipeline definitions, environment-file contents, source control and logs.

The standard Compose deployment includes `utils-email.yaml`. Start it through
`uv run infrastructure/dev/galadril.py up`. The CLI creates private
`.local/dev/utils-email/private` and persistent `.local/dev/utils-email/data`,
preserves keys and configures runtime UID/GID and mounts. Keep the plugin image
pinned to a reviewed release digest for production. See the
[local CLI guide](../../../infrastructure/dev/README.md) for credentials and TLS.

The service shares the existing `api-proxy` network namespace and listens on
loopback only, with no published email port. Gateway and Scribe receive only
the shared `service-key` file; only the worker receives the encryption keyring.
The fixed private routes to Gateway and Scribe are included in the local proxy
configuration. Do not expose `/operation` or `/utils/draft` publicly.

For Kubernetes, render `infrastructure/kubernetes`; the email worker and Gateway/Scribe
key mounts are included in the base. The previous `with-utils` overlay is replaced by the base deployment. Supply Secrets
`utils-email-private` (keyring plus optional OAuth/ingestion files) and
`utils-email-service-key` (service-key). Pin the plugin image to a
reviewed release digest before applying it. The base adds a persistent
volume, one `Recreate` worker, restricted filesystem permissions, probes,
non-root execution, and ambient workload authorization. Gateway and Scribe
must be rolled out with their shared-key patches. Optional
`utils-email-settings` ConfigMap entries configure non-secret file paths and
operator network allowlists. Do not put actual credentials in that ConfigMap.

Common worker configuration:

| Variable | Purpose |
| --- | --- |
| `UTILS_DATABASE_PATH` | Persistent SQLite database path |
| `UTILS_KEYRING_FILE` | Private JSON keyring, `active` and base64 32-byte `keys` |
| `UTILS_SERVICE_KEY_FILE` | Shared private authentication key file |
| `UTILS_GATEWAY_URL` | Internal Gateway origin for live authorization |
| `UTILS_SCRIBE_URL` | Internal Scribe origin for draft generation |
| `UTILS_OAUTH_CLIENTS_FILE` | Optional private provider application JSON |
| `UTILS_INGESTION_FILE` | Optional private per-tenant ingestion configuration |
| `UTILS_ALLOWED_NETWORKS` | Optional comma-separated operator CIDR allowlist |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Existing OTLP collector |

IMAP requires verified TLS on 993; SMTP requires implicit TLS on 465 or
STARTTLS on 587. DNS addresses are validated and pinned before connecting.
Private/loopback/link-local destinations are rejected unless an operator
explicitly allows the specific network. OAuth issuer endpoints are fixed.
Proxy environment variables do not redirect private HTTP calls.

## Google and Microsoft consent

Register a confidential OAuth application with each provider and an exact public
HTTPS redirect URI `https://<dashboard-host>/settings/email`. Configure private
`UTILS_OAUTH_CLIENTS_FILE` as an object keyed by `google` and/or `microsoft`; each
entry has `client_id`, `client_secret`, `redirect_uri`. These app secrets belong
to the deployment; users connect their own accounts through consent. Provider
buttons appear only for configured applications.

Consent uses PKCE S256, a ten-minute single-use state tied to the active tenant
and user, server-side code exchange, encrypted refresh tokens and serialized
token rotation. The callback removes code/state from the address bar before
submitting them. Google needs the mail scope; Microsoft needs delegated
IMAP/SMTP access and offline access. Provider verification, administrative
consent and SMTP enablement remain deployment prerequisites. Live provider
consent must be checked in the target environment before enabling production
accounts; repository tests use protocol servers and mocked token endpoints.

## Correspondence identity

Correspondence is linked from the RFC `Message-ID`, `In-Reply-To` and `References`
headers. The thread identifier is its root Message-ID. A durable number identifies
each distinct message in that thread; importing it again or collecting an email
sent to the same mailbox reuses that number. The optional signature reference is
`<root-message-id>/number`. Removing it from the body does not break the links.
Collected messages can be searched by their Message-ID or thread identifier.

The Messages card opens a graph of the thread, recorded Scribe analyses, ingestion
receipts and up to 100 recent pipeline executions visible to the user. A source
link uses native ingestion metadata, never arbitrary text inside a message.
The graph contains up to 128 messages; legacy correspondence is indexed in
bounded batches. Original `GDR-` references remain in internal metadata for
compatibility with already ingested records.

## Optional opening signals

Opening tracking is off by default. A user may enable it on an individual draft
or in their reply profile. That choice is part of the approved draft revision;
changing it requires approval again. Scribe can propose content but cannot enable
tracking or read opening signals through delegated tools.

The optional `utils` Rust service `tracker` serves only `/healthz` and
`GET /email/open/<opaque>.gif`. It returns the same transparent GIF for valid and
unknown tokens. Its dedicated capture key can only submit opening signals to the
private worker; it has no mailbox database, account keyring or service credential.
It bounds concurrent forwarding to 32 requests with a three-second deadline and
exports aggregate outcomes through OTLP without IPs, tokens or request URLs.

### Named Cloudflare tunnel for local installations

Create a named, remotely managed Cloudflare Tunnel in your own account, following
[Cloudflare's setup guide](https://developers.cloudflare.com/tunnel/get-started/).
Choose a permanent hostname such as `mail-tracker.example.org`. Configure its
published application with service **HTTP → `127.0.0.1:8096`**, path
`^/email/open/[a-f0-9]{64}\.gif$`, and no other published routes. Keep this hostname
and tunnel active for at least the 90-day history lifetime of already sent mail.
Disable caching for this hostname and preserve the `CF-Connecting-IP` header.
Do not attach an interactive Cloudflare Access login to the pixel route.

Store the tunnel token in `.local/utils-email/cloudflare/token` with mode `0600`,
inside a directory with mode `0700`. This token is separate from every Galadril
key and is mounted only in `cloudflared`; never place it in Compose arguments or
tracked configuration. The connector reads it with
[`--token-file`](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/).
No tunnel or token is created automatically.

Fresh email initialization creates `tracking-key`. For an existing installation,
add only that missing key without rotating any credentials:

```sh
uv run --no-project plugins/src/galadril_plugins/utils/provision.py \
  --tracking-only .local/utils-email/private
```

Set the following in the untracked `infrastructure/docker/.env` after the tunnel
and hostname have been configured:

```dotenv
GALADRIL_UTILS_TRACKING_PUBLIC_URL=https://mail-tracker.example.org
GALADRIL_UTILS_TRACKING_KEY_FILE=/run/galadril-utils/tracking-key
GALADRIL_UTILS_TUNNEL_TOKEN_FILE=../../.local/utils-email/cloudflare/token
```

Use the published tracker image tag matching the other Galadril images through
`GALADRIL_UTILS_TRACKER_IMAGE`. Then add `--profile email-tracking` to the usual
Compose command and run `pull`, followed by `up -d --no-build`. The tracker and
connector have no published host ports and share the private proxy network
namespace. Cloudflare IP mode refuses a non-loopback listener and trusts exactly
one valid `CF-Connecting-IP` header only from the loopback connector, ignoring
`X-Forwarded-For`. In direct socket mode, it uses the socket peer instead.

For Kubernetes, use the optional
`infrastructure/kubernetes-overlays/email-tracking` overlay instead of the base directory.
Set its `settings.env` public origin, pin the tracker image for your release, and
create separate Secrets `utils-email-tracking-key` (file `tracking-key`) and
`utils-email-cloudflare` (file `token`) from your private files. The tracker and
connector are sidecars, without a public Service. A waypoint authorization rule
permits its dedicated service account only `POST /tracking/open` on port 8095.

Leave the public origin empty when tracking is unconfigured. Before enabling it
on real mail, request a test pixel through the public hostname and verify an
opening signal on an explicitly opted-in test delivery. A successful private
health check alone does not prove tunnel or DNS reachability.

The **Opening signals** action on a tracked delivery lists timestamps and observed
IP addresses. Each image request is a signal, not proof that a person read the
email: image blocking can hide opens, while privacy proxies and security scanners
can produce or consolidate requests. Multiple recipients share the delivery's
pixel, so an event cannot identify which recipient opened it. The address comes from the Cloudflare edge in tunnel mode; privacy proxies can
still hide the recipient's original address.

IPs and pixel secrets are encrypted at rest; lookup tokens are hashed. Reading
history requires the connection's live `read` permission, with tenant isolation.
History is delivered in pages of 25, capped at 1,000 signals per delivery and
purged after 90 days. Pixel requests contain no sender, recipient, tenant or
document reference. Application traces use a fixed route label rather than the
opaque token.

## Ingestion and pipeline routing

Ingestion is opt-in per connection. Provision a service identity restricted to
the intended tenant and input group, then mount its renewable bearer-token file.
The JSON at `UTILS_INGESTION_FILE` maps each tenant ID to `settings` and
`token_file`. Settings are `gateway_url` (authenticated public `/graphql`),
`storage_origins` (exact allowlisted origins), `tenant_id`, `subject`, `group_id`
and optional SDK bounds. Map key and `tenant_id` must match. The service rereads
the token file for each mutation; external rotation can replace it atomically.

Use the SDK's native staging/upload/finalization delegation. Attachments are
committed before their email envelope, and ingestion checkpoints advance only
after finalization. Redelivery can happen after an uncertain failure; downstream
processing should deduplicate the stable source name/message identity. Once
ingested, data access follows the destination's native ingestion/group policy,
so choose that group deliberately. Account credential grants are never copied
to ingested documents.

Add the **Email accounts** plugin input (`utils.input.email`) to the pipeline.
It uses `schemas/avro/email.avsc`, `source_kind: email`, the configured group and
a stable email JSON filename pattern. Native regex, metadata and Scribe routing
can inspect `record.raw_payload.content` and metadata fields such as
`record.raw_payload.metadata.subject`, `sender`, `date`, `account_id` and
`thread_id`. [Native branch routing](../architecture/pipeline_routing.md)
continues to enforce linked-pipeline permissions, cycles and composition limits.

## Durability and operations

SQLite uses WAL, foreign keys, full synchronous commits and a process lock.
Run one worker per database on persistent local storage. Back up the database
with a consistent SQLite backup mechanism, together with all retained keyring
versions; copying only the live main file can miss WAL transactions. Credentials,
message content, drafts, response profiles and pending OAuth payloads are
AES-256-GCM encrypted with tenant/account/purpose binding. Basic connection
metadata, ACLs, cursor/status and audit metadata are intentionally queryable.
Do not delete old key versions while encrypted rows or backups still use them.

Collection resumes from UIDVALIDITY/UID checkpoints. Remote message size is
bounded before allocation; collection, MIME parts, lists and outgoing attachments
are bounded. One poll fetches at most 25 messages per folder, searching bounded
UID windows; large historical imports progress over multiple polls. The default
poll interval is 120 seconds. List previews omit attachment contents and truncate
bodies; explicit message/draft reads return the full bounded content.

OTLP reports operation outcomes and worker lifecycle without mail text, prompts,
passwords, refresh tokens or exception payloads. Audit events record the actor,
connection, action and time. `needs_attention` means a provider, authorization,
model or ingestion operation needs operator/user review; successful earlier
checkpoints remain durable. Calendar/contact adapters and provider-side
move/archive/delete operations are separate future capabilities.

Manual server settings accept DNS hostnames rather than web URLs. Before moving
to sign-in, the worker verifies public/policy-approved addresses, pins them and
checks certificate-verified IMAP TLS and SMTP TLS/STARTTLS without credentials.
The browser validates syntax, while actual socket checks run on the worker.
Returning from sign-in restores manual server settings when they were entered;
automatically discovered settings can skip that step.
