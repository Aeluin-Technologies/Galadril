# Document retrieval and ESKG

Document search is a separate, rebuildable projection of authorized evidence. ESKG
continues to model entities, states, events and causal relationships. Document
chunks and extracted relationships never enter `entity_embeddings`, entity
resolution, Vision sinks or their ingestion topics.

## Authority and lifecycle

Gateway owns source records, S3 access and authorization. A durable Pengolo upload
atomically queues indexing; temporary uploads are excluded. Existing durable files
are queued during migration. The same `DocumentRetrieval` application port serves
GraphQL, chat tools and future plugin integrations.

Three tenant-protected tables hold units, features and indexing jobs. Units retain
the immutable source checksum, parser location, revision and extracted relationships.
Deleting or expiring the authoritative file removes access immediately. Database
foreign keys cascade cleanup when the source is deleted. Published results must
still match the current file checksum and published revision.

Workers claim jobs with `SKIP LOCKED`, renew the five-minute lease every 30 seconds,
and reject stale publication. Indexing has a fifteen-minute job deadline, bounded
input (25 MiB), text (1 MiB), pages (256), chunks (2048) and attempts (8). After parsing, all chunks are published for lexical search before
model loading and enrichment. This checkpoint keeps the active lease and reports
`enrichment_pending`; native inference failures cannot hide the extracted text.
Transient model failures are retried with backoff. Exhausted jobs expose a failed status when no usable revision
exists. Explicit reindex requests preserve an active lease for unchanged content and
restart an exhausted job once its lease has expired.

Scribe receives bounded evidence over workload-authenticated private RPCs. It does
not receive S3 URLs, storage credentials, database credentials or tenant authority.
Project access is checked before embedding, recognition or candidate retrieval;
source access is checked again before returning evidence. Graph expansion uses
only the same authorized, live source scope.

## Parsing and local inference

Text and CSV parsing preserve character spans. PDFium extracts PDF text with page
numbers and serializes native calls. Structural chunking prefers paragraph,
line and sentence boundaries with bounded overlap. Scanned PDFs and image OCR are
not yet wired to this document parser; the existing OCR model adapters remain
available separately. A missing parser or model produces a visible warning.

Document embeddings use the pinned local Qwen3-Embedding-0.6B Q8 profile. Query
instructions, weight checksum, pooling, quantization and dimensions identify the
embedding space. Queries and chunks must share that profile. The Qwen adapter
advertises text support accurately, rejects silent truncation and supports explicit
contextual token-span pooling in its token mode. The document index currently uses
structural chunks, not end-to-end late chunking or multimodal indexing.

Native PDFium extraction retains PDF page numbers and source spans. Pages without
extractable text and PNG/JPEG/WebP images use the existing GLM-OCR adapter locally
on CPU, with pinned artifacts in the document model cache. Images are bounded to
16 million input pixels and resized to at most 1600 pixels per side before OCR.
The native worker retains admission on cancellation until it releases model memory.
No OCR model is loaded for a PDF whose pages already contain text.

The selected direction for visual retrieval is **Qwen3-VL-Embedding**, with one
versioned shared embedding profile for queries, text chunks and visual pages.
This is not enabled by the OCR integration: the currently deployed Qwen3-Embedding
adapter remains text-only. Equal vector dimensions do not make its vectors
compatible with Qwen-VL or SigLIP. A future profile migration must reindex both
modalities and preserve page/image provenance before enabling cross-modal search;
OCR remains complementary for exact quotations and lexical retrieval.

Eru supplies the local knowledge projection:

1. Extract grounded mentions and merge source-local duplicates. Repair an incorrect
   offset only when the copied mention occurs once in the source.
2. Propose bounded nearby pairs and generate their relationships in one batch,
   rather than one model call per pair. Validate allowed endpoints and exact quotes.
3. If no relationship survives, attempt one direct local projection. Require both
   endpoints in an exact supporting quote and reject invented or duplicate edges.

Native JSON grammar constrains generation; Pydantic validates the complete schema
including lengths afterwards. Expanding long bounded strings in repeated native
rules caused a real llama.cpp failure, so grammar compilation omits string-length
bounds while token budgets and output validation retain the limits. Contexts are
reset and serialized between requests. Telemetry records counts, stages and error
types without document contents.

Qwen3-0.6B Q8 is the default extraction model; Qwen3-4B Q4_K_M is selectable locally.
Model artifacts use pinned revisions and local caching. Recognition first uses
specific named mentions with word boundaries; otherwise it invokes the local model.
A configured chat alias is an optional escalation after local failure or rejection.
No chat alias is selected for document processing by default.

## Search and ranking

The public query is `documentSearch(input: DocumentSearchInput!)`. Inputs include
project/file scope, MIME and date filters, mode, result count, cosine threshold and
reranker relevance threshold. `indexDocument(id: String!)` queues an authorized
source. The library UI displays excerpts, source locations, scores and warnings;
Scribe exposes the same search as `document_search`, including to specialist agents.

Lexical mode uses French BM25 through `pg_textsearch`. Plain PostgreSQL development
tests retain a text-search fallback. Semantic mode uses the exact embedding profile
and an explicit cosine threshold. Hybrid mode fuses lexical and dense ranks before
local reranking. Scores retain distinct meanings: cosine similarity, reranker
relevance and graph score are not calibrated probabilities of correctness.

StreamingDiskANN supports 1024, 2048, 2560 and 4096 dimensions. Queries order directly
by the distance operator. The installed extension failed at execution on expression
indexes (`attnum > 0`), so nullable typed generated columns provide physical index
attributes, with dimension-specific partial indexes. Only the matching column is
populated. This currently stores one additional vector representation per feature;
measure its storage cost when scaling. Unsupported dimensions use exact cosine.
The database regression executes `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` and checks
for the expected `Index Scan` for every supported dimension.

Associative mode runs the pinned official HippoRAG indexing and retrieval engine
with Galadril's grounded facts, local embeddings and recognition adapters. It uses
passage/phrase nodes, synonym edges, upstream seed weighting and personalized
PageRank over the authorized candidate view. Local reranking and graph ranks are
fused by rank rather than by adding incomparable raw scores. Failures retain useful
first-stage evidence with warnings.

Explicit source/checksum provenance connects document passages to actual ESKG
entities and source observations. Both state and event permissions are checked
before expansion and again before returning the ranking. Chat tools receive
`eskgRefs` and can explore further through authorized graph/causal tools; generation
provenance retains those permissions for later replay. Phrase strings remain
retrieval hypotheses, separate from canonical ESKG identities. Temporal propagation
and whole-corpus persistent HippoRAG indexing are outside this bounded integration.
See [the retrieval audit](retrieval_audit.md) for the exact scope and benchmarks.

## Configuration and validation

Compose enables a persistent local model cache. Relevant settings are
`SCRIBE_DOCUMENT_MODEL_CACHE`, `SCRIBE_DOCUMENT_ERU_TIER` (`0.6b` or `4b`),
`SCRIBE_DOCUMENT_RERANKER` (`bge` or `qwen`) and optional
`SCRIBE_DOCUMENT_EXTRACTION_MODEL` (an existing chat alias for escalation).
The first local use downloads public weights; prewarm the cache for offline operation.
Allow several GiB of RAM for simultaneous embedding, extraction and reranking.
The default Kubernetes Scribe deployment leaves document models disabled; enabling
its cache requires provisioning storage and increasing its memory budget.

BGE v2-m3 ONNX int8 and Qwen3-Reranker-0.6B Q8 are interchangeable. Their cached
comparison uses identical query/document pairs and records profiles, elapsed time
and peak process RSS. CPU results for short documents do not predict GPU performance,
long-context performance or French legal quality.

The offline benchmark in `tests/e2e` uses attributed UCI Online Retail observations
already ingested by the ESKG lifecycle test. Twenty-four passages and eight questions
share customer IDs and source rows with that test. Real model outputs are cached,
so ordinary regression runs do not recompute embeddings. The live lifecycle test
uploads the corpus through S3, waits for indexing, searches all four modes through
GraphQL, checks Recall@5 and access isolation, then verifies that ESKG counts and
source identities remain unchanged.

Retrieval uses curated source-derived triples to isolate ranking quality. Eru's
actual cached extractions are evaluated separately for annotated-link recall and
latency; this distinction avoids attributing perfect extraction to the ranker.
See the fixture README for regeneration and measurement details.

The design is informed by [HippoRAG 2](https://arxiv.org/abs/2502.14802),
[Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval),
[late chunking](https://arxiv.org/abs/2409.04701) and
[MUVERA](https://research.google/blog/muvera-making-multi-vector-retrieval-as-fast-as-single-vector-search/).
Contextual prefix generation, adaptive semantic boundary models and multi-vector
MUVERA indexing are not enabled by this implementation.

## Indexing progress

The existing leased `retrieval_jobs` row holds a bounded stage and an estimated
percentage. Reading, parsing/chunking, fact extraction, embedding and publication
advance it from 0 to 95; successful publication removes the job and the ready
source projects 100%. These percentages describe processing work, not confidence
or remaining time. Early lexical publication remains available during enrichment.
Retries and exhausted jobs have distinct states instead of reporting completion.

Gateway projects progress with each authorized library page in the same SQL
statement. Updates validate the source checksum, expiry and current lease; a
stale worker cannot update another attempt or a deleted file. Integer estimates
bound progress writes independently of the number of chunks.

The visible library refreshes one page of at most 50 files every three seconds,
rotating across pending files already loaded by the user. Refreshes bypass cached
progress and retain pagination and previews. Hidden tabs, inactive library views,
terminal jobs and completed indexing stop issuing progress requests.
