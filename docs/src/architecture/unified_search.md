# Unified search and ingested document evidence

Implementation contract: the command palette is the shared entry point for
navigation, authorized actions, source documents and ESKG evidence. The existing
official HippoRAG integration remains a dependency of this work.

The current increment implements shared built-in navigation descriptors, bounded
browser requests and fuzzy search over already loaded Pengolo titles/messages.
The unified pipeline source catalog, multimodal source index and server-side
conversation/message provider below are design requirements, not shipped features.
Existing library retrieval and ESKG search keep their current scope.

## Source authority and projections

One durable source catalog must represent both library uploads and objects accepted
for pipeline analysis. Keep a single chunk store and feature store. Extend the
indexing queue into the durable source catalog rather than introducing parallel
document copies or separate content indexes for each product surface. Library
records remain presentation/retention authority for library sources; raw objects
retain their independent `raw` authorization for pipeline sources.

An immutable source revision records tenant, source kind, storage locator, object
version, MIME type, size, checksum algorithm and indexing status. S3 ETags are not
SHA-256 checksums. Workers pin the object version during reads and compute/verify
the content checksum. Replacements, deletion and permission changes invalidate
searchability before derived graph or vector results can be consumed.

Source registration is independent of successful ESKG analysis. Files with failed
or absent analyses can still be found and inspected. Object discovery is idempotent
per tenant/object revision and must not queue the same file once per parsed row.
Kafka carries immutable source references and trace context, not document bodies,
credentials or presigned URLs. Existing ingestion lineage supplies the source
object identity; ESKG observations retain that explicit binding.

## Content, models and authorization

Text/PDF/OCR chunks retain exact spans or page/region coordinates. Images retain
their original source and explicit image units. Parsing failures and unsupported
formats have visible status; filenames are not substitutes for content indexing.

Local text and image encoders expose fingerprinted profiles. Query and document
vectors must share a profile; incompatible profiles are fused by rank, never by
raw cosine values. The existing Qwen text profile cannot encode images. A local
multimodal profile supplies description-to-image retrieval. Image-query upload is
a later extension of the same typed query contract.

Document authorization is independent of entity-state/event authorization. Filter
source evidence before recognition, graph propagation or model context creation;
recheck source revisions and rights before reads/previews and durable replay.
Tenant filtering alone never grants raw-document access. No shared similarity
result may reveal an unauthorized source or influence an authorized graph view.

## Search surfaces

The command palette immediately searches a compact local navigation/action catalog
and local query history. In parallel, a rotating accessible loading indicator stays
visible until Gateway returns authorized page, plugin capability, document and ESKG
results. Local navigation remains usable while that request is pending. Deduplicate
local/remote destinations by stable capability ID, never by translated label.

Gateway registers compact page/action descriptors, not rendered HTML or live page
data. The same versioned built-in descriptors seed local shortcuts. Semantic
descriptor vectors are computed at indexing time; reuse the query embedding already
needed for remote document search. Query debouncing, bounded candidate retrieval
and a short reranking list avoid inference for each keystroke or each destination.

Normalize case, accents and token order, and register
explicit French/English aliases for terms such as administration, users, people,
permissions and role creation. Respect live administrative visibility. An action
opens the appropriate authorized screen or draft; search never executes writes.

Document results show title, source type, excerpt/preview, location and indexing
status, with links to actual authorized ESKG identities and analyses when present.
An unanalysed document has no invented ESKG ID. Selecting a document opens a detail
view with its original content and linked analyses. Scribe uses the same retrieval
application port and may read the source or follow its ESKG references.

## Conversation search and memory budgets

The browser reuses authorized Pinia snapshots for immediate fuzzy title/message
matches. Fuse.js indexes only a bounded projection: at most 128 conversations,
their ten latest loaded messages, 192 rows and 128 KiB of UTF-16 searchable text.
Only 512 cached chat headers are scanned. Message text is capped at 2,048 characters;
no search triggers history prefetch, downloads attachments or indexes live agent
activity. Index construction is lazy and reused across keystrokes. Scope/version
changes, cache updates and access invalidation discard the index. Temporary chats
are excluded. These local results are explicitly partial, with exact chat/message
links; fuzzy distances are not confidence probabilities.

Server-side conversation/message search remains a proposed additional provider.
Start with indexed text and precise message links under existing personal/project
authorization. Prefer indexed expressions or generated projections on existing
records to copying message bodies into another store. Attachments reference the
same document projection; they never create another copy of the file index.
Temporary conversations remain outside the durable search projection. The browser
must not download all histories to make local search appear exhaustive.

Caches have global byte/entry limits and expiry, rather than an unbounded allocation
per user or tenant. Share immutable plugin/catalog revisions. Cache bounded query
vectors and candidate IDs/scores, avoiding complete conversation histories and file
bodies. Validate current authority before resolving cached candidates. Invalidation
tracks source revisions, retention, access changes and catalog/model revisions.
Model residency has a separate resource budget from caches. OTLP records latency,
queueing, cache bytes/hits/evictions and rejected admission without query contents.

### Proposed provider and cache policy

Start without a shared result cache. Indexed database reads and a process-wide
immutable descriptor catalog already avoid loading plugin implementations,
conversation histories or document bodies for each search. Add caching only after
measuring repeated work; a cache must never be required for acceptable base load.

| Resource                              | Initial bound                                                       | Lifetime and invalidation                                                        |
| ------------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Built-in descriptors                  | 256 entries, 128 KiB source manifest per process                    | Immutable build version; no tenant copies                                        |
| Browser remote results                | 20 per page, 100 retained                                           | Clear on query, identity or access changes; abort superseded requests            |
| Browser navigation shortlist          | 12 entries                                                          | Derived from the compact catalog; deduplicated by stable ID                      |
| Local conversation index              | 192 rows, 128 KiB searchable text                                   | Reused across keystrokes; discarded with the authorized workspace cache          |
| Existing workspace request caches     | 32 entries and 1 MiB estimated weight per store; 16 coalesced loads | LRU admission, expiry sweep on use, no oversized admission; four existing stores |
| Optional query-vector/candidate cache | 32 MiB total and 512 entries per process                            | At most 60 seconds; evict by bytes and count, not TTL alone                      |
| Reranking candidates                  | Existing document shortlist, at most 64 passages                    | Request-scoped; no full file or chat retained                                    |

The shared server cache numbers are proposed configurable admission limits, not
measured sizing guarantees. Browser request caches already enforce the listed
entry/weight limits; their JSON weight accounts for keys, strings and estimated
object/array overhead and rejects overly deep/large structures. Engine-specific
heap usage and the Pinia data already rendered by the product are separate from
this cache weight. Account for metadata and container overhead in addition to vector
buffers. A 4,096-dimensional float32 vector alone uses 16 KiB; 512 such vectors use
8 MiB before any other allocations. Oversized entries bypass the cache. Apply
per-tenant admission/fairness within the global cap, never one cap per tenant.

For messages, persist a compact lexical projection keyed by tenant, conversation,
message and revision. Search titles and message text through database indexes;
resolve only a bounded shortlist of excerpts after checking personal/project
authority. Retain the exact message ID for navigation. Attachment occurrences
reference canonical source IDs and are deduplicated before ranking. Semantic
message indexing is a separate opt-in later stage, not a prerequisite for title or
text search, and temporary chats have no durable projection.

Each provider needs a concurrency limit, cancellation, a deadline and a partial
status (`completed`, `unavailable`, `timed_out`). A slow semantic provider must not
delay local navigation or discard completed lexical results. Coalesce identical
authorized in-flight requests before adding cache residency. Keys include tenant,
identity/access scope, query filters, catalog/index/model revisions and language;
cache entries hold identifiers/scores, never decisions to grant access. Resolve
and authorize candidates again on every use, including after a cache hit.

Measure provider p50/p95 latency, queue depth, cancellation, candidate counts,
resident bytes and cache admission/eviction through OTLP. Benchmarks must include
cold caches, many tenants, repeated queries, cancellation storms and permission
revocation during a pending search. Do not record query text or message bodies in
telemetry.

The deterministic browser-domain benchmark uses 32 distinct synthetic French
topics with one deleted query character each. It measures Recall@5 and reports
index/query timings without a hardware-dependent latency assertion. It is a typo
regression fixture, not a semantic, multilingual or whole-history quality claim.

## Verification

Use TDD for source lifecycle, cross-tenant/source/ESKG permission boundaries,
profile separation, image-description retrieval, command aliases and navigation.
Cache actual local inference outputs for deterministic regression benchmarks.
Extend the existing real upload/pipeline/search lifecycle and browser scenarios;
do not add duplicate fast copies of the full E2E orchestration.
