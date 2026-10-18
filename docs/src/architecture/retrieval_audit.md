# Retrieval audit and projection boundaries

Status: implementation audit and benchmark reference; the isolated upstream service,
Kafka projection protocol, and chatbot write commands below are proposed contracts.

## Algorithm identity

The current `hipporag2` API mode is a bounded associative retrieval implementation,
not an equivalent implementation of the complete upstream HippoRAG 2 pipeline.
The audit pins [upstream commit d5c8329](https://github.com/OSU-NLP-Group/HippoRAG/tree/d5c8329422e0a0b834a15874545cb6a74b4f9b26).

| Mechanism | Galadril document retrieval | Upstream reference |
| --- | --- | --- |
| Passage and triple vectors | Local Qwen, immutable profile | Separate passage, fact and phrase stores |
| Recognition | Grounded named anchors, local Eru, optional configured escalation | LLM fact filter after top-five linking |
| Graph scope | Authorized, bounded query subgraph | Persistent corpus graph |
| Phrase edges | Normalized surface text | Normalized phrases and embedding synonym edges |
| Passage membership | Repeated fact endpoints add edge weight | One membership edge per distinct phrase |
| Phrase seed weights | Recognized scores | Mean fact score divided by phrase document frequency |
| Dense seeds | Normalized global 5% budget | Min-max dense score multiplied by 0.05 per passage |
| Empty recognition | Dense restart on the subgraph | Direct dense retrieval fallback |
| Final ranking | PPR plus external passage reranking | Passage PPR scores |
| Real entity identity | No automatic identity assertion | Surface phrases are not canonical ESKG identities |

These differences affect ranking and scaling. An improved score on a small fixture
would not establish algorithmic equivalence. Do not advertise the existing mode as
a faithful reproduction of the paper or as an upstream performance result.

`//tests/e2e:upstream_retrieval_test` reuses nine unchanged official graph methods,
with MIT attribution and AST fingerprints. It includes passage/fact construction,
synonym edges, frequency-weighted seeds, upstream dense fallback and PRPACK PPR.
Only the small-corpus KNN math uses NumPy instead of Torch. Cached local extraction,
recognition and embeddings hold inference fixed: this is a **graph-kernel ablation**,
not an execution of the full official OpenIE, prompt, persistence or QA pipeline.

The ESKG variant adds customer identity nodes linked through the same source-row
observations as the ingestion E2E. It does not use relevance labels as seeds. Both
variants use exactly the same authorized passages. Identity/source mismatches are
rejected before propagation. This first ablation does not measure temporal ESKG
traversal, causal reasoning, or a legal-domain workload.

The measured graph-kernel Recall@5 is 0.8333 without and 0.875 with those ESKG links
on eight retail questions. The separate HotpotQA regression uses forty fixed hard
questions (twenty bridge, twenty comparison) and actual cached local model outputs.
It compares BM25, dense, hybrid and BGE reranking, with a predefined paired hypothesis
and uncertainty intervals. It does not supply gold triples to claim full official
HippoRAG performance. See `tests/e2e/fixtures/HOTPOTQA.md` for the protocol.

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
cross-encoders rerank the same candidate evidence. Cosine, BM25, cross-encoder scores
and PPR have different meanings; none is a calibrated confidence percentage.
The cached benchmark compares BGE and Qwen on identical pairs and reports Recall@5,
native CPU timing and peak process RSS. The live E2E additionally exercises actual
`pg_textsearch`, DiskANN queries, GraphQL authorization and index lifecycle.

## Prefer an isolated official engine

Reuse upstream rather than maintaining another graph implementation. The complete
upstream package currently pins Torch 2.5.1 and Transformers 4.45.2; the workspace
requires Torch >=2.11 and Transformers >=5.8. A separate dependency lock and OCI
microservice avoids forcing incompatible runtime dependencies into Scribe or Eru.
The offline reference deliberately does not import that complete package.

The service must receive a Gateway-authorized evidence view before recognition,
synonym expansion or PPR. Filtering only final results is insufficient: forbidden
documents could otherwise influence ranks or model prompts. A persistent index is
partitioned by tenant and authorization-view revision, or traversed through an
authorized induced subgraph. Index identity includes source revision, model profile,
chunking policy and ACL revision. Revocation fences stale snapshots and caches.

Upstream currently supplies local Parquet and optional vector-store adapters, not
our PostgreSQL authorization/lifecycle contract. Keep source metadata authoritative
in Gateway and treat upstream indexes as rebuildable projections. Never deserialize
user-supplied graph pickle files. Its optional MCP HTTP server has no authentication;
do not expose it directly. Private workload identity, bounded requests, admission,
OTLP metadata and read-only search remain mandatory at the service boundary.

Switching production retrieval requires a full-package integration test with local
inference adapters, deletion/revocation replay and authorization-view isolation.
The graph-kernel benchmark is deliberately not sufficient evidence for that switch.

## Kafka connects projections, not search requests

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
