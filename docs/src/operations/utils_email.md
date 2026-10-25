# Email accounts with the official utils plugin

The `utils` email service collects multiple IMAP mailboxes, stores mail
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
Instructions, tone and an HTML signature are configurable. Signatures are
sanitized and appended after generation, with a plain-text alternative; scripts,
images and tracking resources are removed. Selecting an API model sends the email
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
New response profiles start with `Ref. : <code>{{ ref }}</code>`. Signature fields
are substituted at sending time, never evaluated as code. The preview uses
example values. Existing custom signatures remain unchanged.

| Field | Value |
| --- | --- |
| `{{ ref }}` | Stable `GDR-` tracking reference |
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

The directory is created with mode `0700`; `service-key` and `keyring.json` use
`0600`. The command refuses to overwrite existing files and never prints keys.
Set file ownership for runtime UID/GID 1000. Prepare a separate persistent data
directory owned by that runtime. Mount credentials read-only and keep keys out
of pipeline definitions, environment-file contents, source control and logs.

The standard Compose deployment now includes `utils-email.yaml`. Run
`bash infrastructure/docker/dev-environment.sh` before the first start. It creates
private `.local/utils-email/private` and persistent `.local/utils-email/data`,
preserves existing keys across restarts and stores only paths/runtime IDs in
`.env`. The worker uses the selected non-root runtime UID/GID; production defaults
to 1000. Keep `GALADRIL_PLUGINS_IMAGE` pinned to the reviewed release digest for
production, alongside the other service image overrides.

```sh
docker compose --env-file infrastructure/docker/.env \
  -f infrastructure/docker/docker-compose.yaml up -d
```

For the Nuxt development stack, also include `infrastructure/docker/nuxt-dev.yaml`
and retain any existing local overrides. Its generated `.local/nuxt-dev-envoy.yaml`
must include the reviewed private routes from `infrastructure/envoy/local.yaml`,
including `/utils/draft` and `/internal/utils/authorize`. Validate Envoy's complete
configuration before restarting it. Services sharing its network namespace,
including the identity services and Studio, must restart after the proxy.

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
