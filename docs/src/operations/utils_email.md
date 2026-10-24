# Email accounts with the official utils plugin

The optional `utils` email service collects multiple IMAP mailboxes, stores mail
durably, delivers opted-in messages and attachments through native ingestion, and
prepares or sends replies through verified TLS SMTP. Google and Microsoft accounts
use OAuth consent with IMAP/SMTP; other providers use passwords or app passwords.
Providers must enable these protocols. Gmail REST history and Microsoft Graph
delta synchronization are not adapters in this version.

Open **Email accounts** in the account menu (`/settings/email`). Each connection
has its own folders, collection interval, ingestion choice, response profile and
access grants. On first collection, the default is **new messages only**. Choose
**all available messages** to import history; review automation settings before
doing so. Collection uses read-only IMAP and does not mark messages as read.
Local category labels organize the collected copy; they do not modify provider
folders or flags. Removing a connection removes its local mail, drafts and tokens,
without deleting messages at the provider.

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
| `reveal` | Explicitly retrieve credentials or provider tokens |
| `share` | Inspect connection grants and audit events |
| `approve` | Approve an exact draft revision |
| `send` | Send an approved draft revision |
| `automate` | Configure explicitly authorized automatic replies |

Any granted right also makes basic connection metadata visible. Only the owner
can change grants, including granting `reveal`; an editor cannot grant it to
themselves. An editor or deleter does not need `reveal`. Existing secrets are
absent from normal API responses, pipeline YAML, catalog metadata and dashboard
state. Replacement credentials are write-only. Changing the sender or provider
endpoint requires complete replacement credentials, so old secrets cannot be
redirected to a different host. Concurrent changes use revision checks.

Generating a reply from collected mail requires both `read` and `use`; it does
not require permission to edit the connection. Disabling a connection stops
collection and prevents sending, including already approved drafts.

Secret reveal is deliberate and temporary in the dashboard. It is never needed
for ordinary connection management. Account owners should grant it sparingly.
The worker also rechecks active tenant membership through Gateway before
background collection and before sending. Connection ACLs are an independent
boundary; they do not bypass native tenant authorization.

## Replies and automation

Select any configured **Scribe model alias**, local or API-backed, and explicitly
list acceptable fallback aliases. If the primary disappears or becomes
unavailable, only these fallbacks may be tried. With no usable approved model,
generation fails without sending. Registered Skills, instructions, language,
tone and signature are configurable. Selecting an API model sends the email
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

SMTP cannot guarantee exactly-once delivery across a lost acknowledgment.
Before contacting SMTP, the service durably claims the approved draft. A crash
or uncertain response leaves `sending` or `uncertain`, blocking blind retries.
Check provider logs or Sent mail using the displayed stable Message-ID before
deciding whether to compose another message. There is no automatic resend or
delivery reconciliation mutation in this version.

## Provision and deploy

The plugin is discovered from the reviewed package at startup, but discovery
does not start its listener. Deploy the same reviewed bundle to Scribe, Vision
and the plugin worker, and restart those runtimes after installation.

Generate private deployment files once:

```sh
bazel run //plugins:utils_init -- /absolute/private/utils-email
```

The directory is created with mode `0700`; `service-key` and `keyring.json` use
`0600`. The command refuses to overwrite existing files and never prints keys.
Set file ownership for runtime UID/GID 1000. Prepare a separate persistent data
directory owned by that runtime. Mount credentials read-only and keep keys out
of pipeline definitions, environment-file contents, source control and logs.

For local Compose, add `infrastructure/docker/utils-email.yaml` to the normal
Galadril Compose files and set:

- `GALADRIL_PLUGINS_IMAGE`: reviewed image pinned by immutable digest;
- `GALADRIL_UTILS_PRIVATE_DIR`: the generated private directory;
- `GALADRIL_UTILS_DATA_DIR`: the prepared writable state directory.

The service shares the existing `api-proxy` network namespace and listens on
loopback only, with no published email port. Gateway and Scribe receive only
the shared `service-key` file; only the worker receives the encryption keyring.
The fixed private routes to Gateway and Scribe are included in the local proxy
configuration. Do not expose `/operation` or `/utils/draft` publicly.

For Kubernetes, render `infrastructure/kubernetes/with-utils`. Supply Secrets
`utils-email-private` (keyring plus optional OAuth/ingestion files) and
`utils-email-service-key` (service-key). Pin the overlay's plugin image to a
reviewed release digest before applying it. The overlay adds a persistent
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
