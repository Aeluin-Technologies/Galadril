# Retrieval audit and projection boundaries

Status: official retrieval engine and ESKG observation bridge implemented. A dedicated
Kafka projection protocol and chatbot write commands below remain proposed contracts.

## Official engine and local adapters

The `hipporag2` API mode now executes `HippoRAG.index()` and `HippoRAG.retrieve()`
from the complete [official package at d5c8329](https://github.com/OSU-NLP-Group/HippoRAG/tree/d5c8329422e0a0b834a15874545cb6a74b4f9b26).
`third_party/hipporag/upstream/PROVENANCE.json` fingerprints every Python source.
The only upstream patch makes unused cloud/Transformers providers lazy imports;
index construction, synonym KNN, linking, dense fallback and PRPACK PPR are unchanged.
The former Galadril approximation has been removed from production code and tests.

The integration deliberately supplies Galadril components: grounded Eru facts,
Qwen vectors and local recognition selections, with optional configured escalation
remaining under the existing Scribe policy. It replaces upstream OpenIE and DSPy
provider calls with typed evidence adapters. This is the official retrieval engine
with local adapters, not a reproduction of the paper's models or prompt evaluations.
Scribe continues to own answer generation; upstream QA and its unauthenticated MCP
server are not exposed.

Upstream passage/fact/phrase stores are built from a Gateway-authorized view of at
most 256 passages. One exact view is cached in a private temporary directory;
source/fact/profile/link changes replace it and close its stores. No external index
or pickle path is accepted. Chunk/fact embeddings produced during indexing are
reused, alongside a bounded cache of local phrase vectors. Database/S3 authority
stays in Gateway. This bounded candidate-view integration is not a persistent
whole-corpus HippoRAG index and does not establish whole-Wikipedia scalability.

## ESKG bridge and authorization

A pipeline can provide `document_evidence` in its raw payload, as one object or an
array of up to sixteen `{source_id, checksum}` objects. IDs are immutable Pengolo
file IDs; checksums are lowercase SHA-256 hex. Vision validates and stores this
provenance on the source event. Gateway matches it to the current file revision,
then follows actual `entity_states` observation identities. Matching names never
create an identity assertion. No new document-to-entity table is introduced.

Before propagation, Gateway checks document ownership/project scope and separately
checks both the ESKG entity-state and event permissions with their actual state type.
Expansion follows shared authorized identities or source observations; discovered
documents and their bindings are checked again. Revoked or changed graph evidence
discards the graph ranking. Expired/deleted files and checksum changes cannot reuse
an old view. Errors preserve usable first-stage evidence and return warnings.

The extension adds distinct ESKG entity and observation nodes to the upstream query
graph, retaining upstream phrase nodes as retrieval hypotheses. It does not promote
phrase similarity into canonical ESKG identity, infer causation, or implement a new
temporal propagation algorithm. Relevant `eskgRefs` are returned to chat/plugin
callers and recorded in generation provenance, preserving revocation checks during
replay. Scribe can follow those references through the existing authorized graph
and causal tools when the question needs broader state/event context.

GraphQL `documentSearch` exposes `includeEskg` (default true for associative mode),
`engine` and `eskgRefs`. `includeEskg:false` provides the plain-engine ablation.
Existing chat and library search routes use the same application port.

## Distinct benchmark claims

`//machine-learning/scribe/tests:official_hipporag_test` executes the complete pinned
package, including its real stores, indexing, retrieval, deletion, empty-recognition
fallback, view replacement and pinned upstream source checksums. It also verifies that the
runtime uses CPU Torch rather than CUDA.

`//tests/e2e:official_memory_test` compares dense, official, official+ESKG and both
variants with local reranking on 32 fixed source-derived questions. Query vectors
and BGE scores are actual cached local model outputs. Facts are controlled,
source-derived triples; recognition uses Eru's named-anchor policy. This isolates
retrieval from extraction quality. The live lifecycle binds the same documents to
actual Vision entity/event IDs; fixture-derived IDs are labeled separately.

The measured Recall@5 values are 0.3177 (dense), 0.8229 (official), 0.8438
(official+ESKG), and 0.8333 for each variant after reranking/rank fusion. Thus ESKG
improves the raw graph ranking on this fixture, but adds no measured final-ranking
gain after fusion. The 32 questions share eight source observations; they are not
32 independent observations and these numbers do not establish statistical or
legal-domain generalization. See `tests/e2e/fixtures/RETRIEVAL.md`.

The older `//tests/e2e:upstream_retrieval_test` remains a separate nine-method
fingerprinted graph-kernel reference. Its eight-question results are not the new
full-package benchmark. The forty-question HotpotQA regression still compares
BM25/dense/hybrid/local reranking, not HippoRAG+ESKG on HotpotQA. See
`tests/e2e/fixtures/HOTPOTQA.md` for its own statistical protocol.

## Chunking, fusion and reranking

The deployed chunker adapts to paragraph, line and sentence boundaries under fixed
character budgets. It is structural chunking, not semantic adaptive chunking.
Its exact source spans and overlap are regression-tested. The short passage corpus
alone cannot establish the retrieval quality of a long-document chunking strategy.

Qwen's default embedding profile uses its trained last-token pooling. Contextual
token-span mean pooling is an experimental separate profile; document indexing does
not currently use it. Qwen is causal: mean pooling its token outputs does not supply
the bidirectional whole-document context assumed by the late-chunking paper. Do not
mix these vectors with the default profile or claim paper-equivalent Late Chunking.

Hybrid search applies reciprocal rank fusion to BM25 and dense candidates. Local
cross-encoders rerank the graph shortlist; associative mode combines their ranks
with graph ranks using reciprocal rank fusion. Raw cross-encoder and PPR scores are
never added together. Cosine, BM25, cross-encoder scores
and PPR have different meanings; none is a calibrated confidence percentage.
The cached benchmark compares BGE and Qwen on identical pairs and reports Recall@5,
native CPU timing and peak process RSS. The live E2E additionally exercises actual
`pg_textsearch`, DiskANN queries, GraphQL authorization and index lifecycle.

## Runtime packaging

The engine is a private Scribe library, avoiding a second deployment and duplicate
inference models. A separate CPU-only Torch lock and Bazel dependency hub avoid
pulling Vision's CUDA graph into Scribe. Shared wheels resolve to the main hub;
large native libraries have explicit OCI layers. The local adapters do not import
upstream's incompatible optional Transformers/provider implementations. The tested
runtime uses the workspace's modern NumPy/Pandas/Pydantic and CPU Torch versions,
not the upstream distribution's old Torch/Transformers pins.

Private workload identity, request budgets, admission, content-free OTLP spans and
read-only retrieval remain at the service boundary. A future independent service
can reuse the same evidence-view contract; it must preserve authorization before
all recognition, synonym expansion and graph traversal.

## Kafka connects projections, not search requests

This is a proposed extension. The current document indexer still uses PostgreSQL
jobs; existing ingestion pipelines already use Kafka. No new source outbox or
independent HippoRAG Kafka consumer is claimed by this integration.

Reuse the existing Kafka/Redpanda deployment and pipeline event envelope. Gateway
commits an immutable source revision and an outbox row in one transaction; an outbox
publisher emits a versioned source event keyed by `tenant_id:source_id`. Consumers
independently maintain document memory and ESKG projections. Do not couple either
consumer's transaction or availability to the other's progress.

Events carry an event ID, correlation/causation IDs, source ID, monotonic revision,
content checksum, object reference, MIME, selected pipeline revision, authorization
revision and projection intent. They carry neither document bodies nor vectors,
credentials or signed download URLs. Consumers resolve object references through
workload identity and tenant-scoped access. Trace context propagates through headers;
telemetry includes identifiers and durations rather than document content.

Delivery is at least once. Each projection atomically records its consumed revision
with the resulting state; duplicates are idempotent, old revisions are ignored, and
deletion tombstones prevent late retries from resurrecting sources. Kafka offsets
advance only after durable application. Bounded retries, dead-letter metadata,
operator replay and per-projection lag/status are observable. ACL changes and
deletions invalidate searchable snapshots before asynchronous physical cleanup.

Synchronous search continues through authenticated APIs. Kafka is not a substitute
for per-request authorization and does not grant permission to ingest into ESKG.

## Future chatbot ingestion and pipeline authoring

Retrieval indexes source evidence without asserting ESKG truth. A future chatbot
write operation is a separate command: `analyze_source(source_id, revision,
pipeline_id, pipeline_revision, target_projections)`. Gateway rechecks the current
actor's source read and pipeline execution permissions, resolves the immutable S3
reference itself, records an audit/outbox entry, then returns a durable execution ID.
The UI can show independent ESKG and document-memory progress and partial failures.

Pipeline creation follows a draft/validate/authorize/execute lifecycle. The draft
references registered input, model, algorithm, MCP, Skill and output capability IDs,
plugin versions and configuration schemas. Validation checks type compatibility,
model restrictions, resource budgets, permissions and availability. The model cannot
invent executable plugin code or bypass approval by embedding instructions in S3.
Creation permission and execution permission are checked separately. Retries bind
to an idempotency key and the reviewed pipeline revision. These chatbot commands
are future work; search and existing file upload do not acquire implicit write rights.
