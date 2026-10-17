# Pengolo workspace

Pengolo is an authenticated client of Gateway. Gateway owns project and library
authorization, retention, idempotency, and replay. Scribe owns background agent
execution. Neither browser sockets nor Gateway request lifetimes own a run.
Scribe receives a generation-scoped capability and sends ordered, idempotent
output batches to Gateway. Every tool call and output read rechecks live access.
Scribe has no database, S3, or IAM credentials.

The existing generic conversation API remains compatible. Pengolo has a separate
contract that requires a catalog alias supporting `medium`, never accepts a
reasoning override, and creates a conversation atomically with its first turn.
The same idempotency key and payload return the original generation; changing
the payload with that key fails. Replay subscriptions never start inference.

PostgreSQL provides tenant RLS, compare-and-swap revisions, ordered event cursors,
and expiring execution leases. Normal state is durable. Temporary state lives in
separate UNLOGGED tables with a 24-hour TTL, never in normal conversation tables
or libraries. Access checks enforce expiration before asynchronous physical
cleanup. A stale execution lease becomes an explicit failure; it is never
silently retried against a provider.

Personal libraries and personal projects require exact ownership, including for
tenant administrators. Shared projects use live effective Galadril role
membership and always use their own library. Moving a chat checks all historical
file dependencies and forbids widening a personal or project document's scope.
Graph evidence is independently reauthorized on replay and reuse. Library scope
never disables GraphRAG, calculation, Python, or authorized MCP tools.

Only the project owner can rename or edit its sharing roles. Existing projects
retain their personal/shared boundary. Permission edits expand both the previous
and proposed role grants through the tenant hierarchy and must preserve access
for every direct or transitive superior of the owner who already had access.
Validation runs under the IAM lifecycle lock, before metadata or authorization
relationships change, and compare-and-swap revisions reject stale editors.

Files use random identifiers and Gateway metadata, with 30-day normal and
24-hour temporary retention. The browser uploads directly to S3 staging using a five-minute SigV4 PUT capability.
The signature binds the exact opaque key, MIME type, disposition, ownership metadata,
SHA-256 checksum and declared length. `If-None-Match: *` prevents overwrites.
Gateway never accepts an object path from the browser or model. Finalization checks
S3 ownership, length, checksum, timestamp and a bounded format signature, then copies
an ETag/version-pinned source to its canonical library key with trusted metadata.
Retries return the same ready file. Case-insensitive display-name collisions receive
` (1)`, ` (2)` suffixes; original names remain metadata.
Staging expires within one hour, while ready retention starts at the S3 upload time.
Durable content-free cleanup records retain opaque object keys even if PostgreSQL
truncates temporary UNLOGGED tables after a crash. Purge runs through the existing
Gateway housekeeping task, including S3 versions and delete markers. Physical cleanup
can be delayed while Gateway or storage is unavailable; access expiration is immediate.
The staging source remains until expiry so an unexpired write-once URL cannot recreate
an untracked object after promotion. Browser downloads use authenticated, noncacheable
Gateway GETs that recheck live permissions; short signed GETs are private Scribe inputs.
Pengolo files do not enter the platform ingestion pipeline, avoiding orphaned
graph indexes or embeddings. Deleting a chat does not delete its library files.

Graph context is a typed canonical object reference. Scribe must resolve it with
authorized tools and traverse its relevant neighborhood before inference. Forced
web search uses only a separately supplied public search query, never the chat,
history, attachments, or graph evidence. Future artifacts have typed envelopes;
scheduled jobs, ontology creation, dashboards, and workflows remain future work.

Pinia caches are memory-only and scoped to the authenticated tenant and actor.
Subscriptions and retries are centralized, ordered by cursor, and invalidated
on identity changes or authorization denial. New chat is a local draft until
the first accepted send. Browser persistence must never retain chat contents.

## Deployment and API

`connectors.s3.public_endpoint` selects the browser-facing HTTPS S3 endpoint, separate
from the private storage endpoint. Local Compose defaults to `AUTH_PUBLIC_ORIGIN`;
Envoy routes only signed PUTs under `/staging/` directly to RustFS, preserving the signed
Host/path and stripping cookies and tracing headers. Staging request URLs are excluded
from tracing; access telemetry records status and duration without query strings.
Other deployments must expose their private staging bucket through the configured S3
endpoint, permit only required upload headers in S3 CORS, and retain private bucket
policies. Provider credentials never reach the browser.

The migration `202610130001_pengolo.sql` creates tenant-RLS workspace, output,
capability and retention tables, with separate UNLOGGED temporary content tables.
The GraphQL workspace exposes `pengoloIdentity`, `pengoloModels`, `pengoloProjects`,
`pengoloChats`, `pengoloChat`, `pengoloLibrary`, `pengoloEvents`, `pengoloStream`
and `pengoloIdentityChanges`. All Pengolo outputs and mutation inputs have named
GraphQL object, input and enum types. Workspace values are never JSON-encoded
strings or JSON scalars. The identity version is an opaque invalidation hint,
not an authorization token.
Owned project edits do not invalidate the owner's in-progress draft. Membership
changes and revisions of projects shared by other owners invalidate cached access.
An authorization denial on an owned project also changes the identity version,
including policy-driven revocation without any role membership change.
Mutations cover send/stop, project editing, chat editing/deletion, temporary closure,
upload reservation/finalization and file download/deletion. `/pengolo/files/{id}` is
an authenticated GET for download or sandboxed preview. Scribe's private generation
submission/cancellation and Gateway's capability-bound output callback are not public.

The model catalogue remains dynamic and filters incompatible reasoning modes.
OpenAI models use Responses API native web search in a separate, bounded request
containing only the explicit public query. Its SDK client retains Scribe's pinned
endpoint and credentials; it receives no history, documents or Gateway capability.
The private agent receives the returned public evidence without a web-search tool.
`PENGOLO_WEB_SEARCH_URL` remains available for a trusted HTTPS search adapter with
other providers. The default output budget is 4096 tokens, including reasoning.
Scribe's detached execution uses the existing process and Gateway persistence;
there is no new worker service, queue, Redis or scheduler. A crashed executor fails
explicitly after its lease expires. Temporary chats are recoverable only during their
TTL and are closed when leaving the temporary workspace. Typed response parts reserve
future artifact output; scheduled tasks, ontologies and workflows are not implemented.

## Client loading and rendering

The canonical operations live in `front/dashboard/app/graphql/pengolo.graphql`
and are validated against Gateway's native schema. Pinia loads identity, models,
projects, chat headers and the selected chat in one deduplicated query. The initial
message page contains ten messages. Scrolling upward requests only the preceding
ten messages, retaining file references and the reader's scroll position.

One `graphql-transport-ws` connection multiplexes identity changes and generation
subscriptions. Durable output notifications wake matching subscriptions after
commit; cursor replay recovers gaps and reconnects without restarting inference.
A two-second server fallback reconciles output written by another replica. Identity
notifications reconcile external IAM writers every thirty seconds on the server.
Every replay and tool operation still checks live permissions. Completion carries
only the matching assistant message and chat revision, so it does not refetch history.

Markdown rendering disables model HTML, scopes deterministic heading and footnote
IDs to each message, highlights code and loads KaTeX styles. Task selections are
revision-checked owner edits on terminal assistant messages. Copy returns Markdown;
table export writes literal cells into XLSX. Charts use bounded declarative `chart`
fences with `type`, `labels` and named numeric `series`, rendered by ECharts with
safe text tooltips. Model JavaScript, HTML and SVG are never executed.

Remote images pass through the authenticated `/media/image` endpoint. It pins
validated public DNS addresses, checks each HTTPS redirect, enforces a deadline
and byte limit, accepts raster signatures and forwards no browser credentials.
Auth, GraphQL and private media responses use private/no-store cache policies,
including proxy-generated authentication errors. Scribe alone may download
Gateway-created private S3 references; ordinary media URLs retain SSRF checks,
and cloud metadata endpoints remain blocked.

## Local S3 provider

Development and Linux E2E use the same digest-pinned RustFS 1.0.1 image.
A single node and a separate `rustfs-data` volume avoid reinterpreting existing
MinIO data. The legacy `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` and port settings
remain accepted by Compose so local credentials do not silently rotate.
MinIO volumes are preserved; existing data must be copied through S3 before
switching a populated deployment. LakeFS, Gateway and the upload proxy resolve
`rustfs:9000`. The pinned `mc` image is only an initialization client.

RustFS is selected for its single-process local deployment, OTLP export and
verified conditional operations. Its stable 1.0.1 Kafka client sends Produce v9,
which Redpanda 26.2 rejects (it supports v0-v7). Bucket notifications therefore
use RustFS's persistent webhook queue and a digest-pinned Bento HTTP-to-Kafka
relay, with no public port and no custom protocol code. The relay preserves
the event body, uses idempotent Kafka writes, exports OTLP traces and acknowledges
HTTP only after Kafka acknowledges delivery.

RustFS allows only the relay's exact internal origin through its outbound SSRF
policy; private and link-local destinations remain denied by default. Storage
waits for the relay's readiness check before initializing notifications.

Ceph RGW also supports S3 and Kafka notifications, but requires a backing Ceph cluster with
MON, MGR and OSD services; that operational footprint is unnecessary for a
personal Mac. This is an architecture choice, not a measured CPU/RAM benchmark.
Sources: [RustFS container deployment](https://docs.rustfs.com/en/installation/container),
[RustFS event notifications](https://docs.rustfs.com/en/operations/event-notifications),
[Bento Kafka acknowledgements](https://warpstreamlabs.github.io/bento/docs/components/outputs/kafka_franz/),
[Ceph architecture](https://docs.ceph.com/en/latest/architecture/),
[Ceph notifications](https://docs.ceph.com/en/latest/radosgw/notifications/).

S3 conformance is tested against the pinned binary: signed length and metadata,
SHA-256 rejection, write-once PUT, conditional destination copy, immutable-source
promotion and repeat finalization. The full pipeline test independently proves
Kafka delivery instead of treating an accepted notification rule as delivery.
These tests must pass before deploying a different storage image.
