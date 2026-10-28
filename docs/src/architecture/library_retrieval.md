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
and reject stale publication. Indexing and renewal remain independently polled:
a renewal waiting for a job-row lock must not suspend the transaction that owns
that lock. Renewal failure cancels indexing; the job deadline cancels both futures.
Indexing has a fifteen-minute job deadline, bounded
input (25 MiB), text (1 MiB), pages (256), chunks (2048) and attempts (8). After parsing, all chunks are published for lexical search before
model loading and enrichment. This checkpoint keeps the active lease and reports
`enrichment_pending`; native inference failures cannot hide the extracted text.
Transient model failures are retried with backoff. Exhausted jobs expose a failed status when no usable revision
exists. Explicit reindex requests preserve an active lease for unchanged content and
restart an exhausted job once its lease has expired.

The work revision survives automatic retries independently from the fencing lease.
Each fully extracted and embedded passage is marked durably; subsequent attempts
reuse those facts, vectors and the parsed checkpoint instead of rereading S3 or
repeating OCR. Progress is monotonic within that work revision. Completing a passage
resets the consecutive-failure budget, so a large document can span several bounded
attempts. Explicit retries of exhausted jobs and replacement content start a new
work revision. The additive migration preserves existing projections; passages
created before completion markers existed are conservatively processed again.

Scribe receives bounded evidence over workload-authenticated private RPCs. It does
not receive S3 URLs, storage credentials, database credentials or tenant authority.
Project access is checked before embedding, recognition or candidate retrieval;
source access is checked again before returning evidence. Graph expansion uses
only the same authorized, live source scope.

## Parsing and local inference

Text and CSV parsing preserve character spans. PDFium extracts PDF text with page
numbers and serializes native calls. Structural chunking prefers paragraph,
line and sentence boundaries with bounded overlap. A missing parser or model
produces a visible warning.

Document embeddings use pinned local **Qwen3-VL-Embedding-2B**, with 1024-dimensional
MRL vectors normalized after trained last-token pooling. The repository revision,
precision, instructions, dimensions and preprocessing bounds identify the profile.
Queries, text passages and image-plus-OCR inputs share this profile. Inference uses
the Transformers backbone directly, without remote Python or a generative head.
Inputs exceeding the 8192-token runtime budget are rejected instead of truncated.
Standalone images retain a searchable visual unit even when OCR finds no text.

Native PDFium extraction retains PDF page numbers and source spans. Pages without
extractable text and PNG/JPEG/WebP images use the existing GLM-OCR adapter locally
on CPU, with pinned artifacts in the document model cache. Images are bounded to
16 million input pixels and resized to at most 1600 pixels per side before OCR.
The native worker retains admission on cancellation until it releases model memory.
No OCR model is loaded for a PDF whose pages already contain text.

PNG/JPEG/WebP indexing sends checksum-verified image bytes from Gateway to Scribe
alongside OCR chunks. The embedding worker limits decoded inputs to 16 million
pixels and bounds visual preprocessing to 512 pixels per side. Text-only reranking
cannot erase a visual candidate's dense relevance solely because OCR is sparse.
Visual vectors are never cached as text-only phrase vectors. Scanned PDF pages
still use OCR text; page-image embedding is not yet implemented for PDFs.

The previous text-only profile remains solely for reproducing recorded benchmark
fixtures through `LegacyTextEmbeddings`; those fixtures do not measure the new
multimodal model. Existing documents need manual reindexing to use the new profile;
there is no automatic migration or fallback that mixes old and new vectors.
`//machine-learning/galadril-inference/tests/models:qwen_vl_embedding_live_test`
is an explicit, network-enabled smoke test using real weights and repository
images. Normal regression tests use small tensors and never download these weights.
The implementation follows the [official Qwen embedding reference](https://github.com/QwenLM/Qwen3-VL-Embedding).

Eru supplies the local knowledge projection:

1. Extract grounded mentions and merge source-local duplicates. Repair an incorrect
   offset only when the copied mention occurs once in the source.
2. Generate up to eight open-information triples in one relation pass, using either
   the remote backend or the local SLM. Additional explicitly mentioned entities
   may be supplied by that relation pass.
3. Require both endpoints in an exact supporting quote. Reject invented or duplicate
   edges and, when Registry supplies an ontology, reject incompatible predicates
   or endpoint types. Empty results do not trigger another local relation pass.

Native JSON grammar constrains generation; Pydantic validates the complete schema
including lengths afterwards. Expanding long bounded strings in repeated native
rules caused a real llama.cpp failure, so grammar compilation omits string-length
bounds while token budgets and output validation retain the limits. Contexts are
reset and serialized between requests. Telemetry records counts, stages and error
types without document contents.

For explicitly local extraction, Qwen3-0.6B Q8 is the default model; Qwen3-4B Q4_K_M is selectable locally.
Model artifacts use pinned revisions and local caching. Recognition first uses
specific named mentions with word boundaries; otherwise it invokes the local model.
Relation extraction prefers the configured service OpenAI model by default and
can be switched to the local model through the environment settings below.

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

Compose enables a persistent local model cache. With
`SCRIBE_DOCUMENT_REMOTE_RELATIONS=true` (the default), Scribe never constructs,
loads or downloads the local Eru generative model, including when credentials are
missing or a remote request fails. The configured provider extracts grounded
entities and relations in a single structured pass. An explicit
`SCRIBE_DOCUMENT_EXTRACTION_MODEL` may select either an OpenAI or vLLM catalog
alias; otherwise `SCRIBE_DOCUMENT_RELATION_MODEL` selects the OpenAI model.
Interactive ChatGPT credentials are not used for background extraction.
Unconfigured or failed extraction remains visible as a bounded warning while
lexical evidence stays available. Fully local extraction requires the explicit
`SCRIBE_DOCUMENT_REMOTE_RELATIONS=false` setting; its model tier is selected by
`SCRIBE_DOCUMENT_ERU_TIER` (`0.6b` or `4b`).

Embeddings, OCR and reranking still run inside Scribe. OCR ownership is unchanged.
They share one native model slot: switching to another model releases the previous
weights under the inference lock, while repeated calls reuse the current model.
Gateway processes at most two documents per tenant together. Native inference
favors bursts of up to four waiting calls on the current model, allowing documents
to share loaded weights without running two heavyweight models simultaneously.
Interactive embedding and ranking requests take priority between native calls.
The HTTP admission limit reserves capacity for interactive work while accepting
up to two background operations. This bounds resident weights at the cost of
checkpoint reloads when work alternates between OCR, embedding and ranking.
`SCRIBE_DOCUMENT_EMBEDDING_COMPUTE_TYPE=bf16` preserves the existing vector profile.
`int8` enables CPU weight-only quantization through TorchAO during checkpoint
loading, with FP32 activation arithmetic to avoid slow BF16 vision kernels on
ARM CPUs without BF16 acceleration. Quantized and reference
profiles are distinct: remove and reupload documents when changing this setting;
no automatic reindex or data migration is performed. Never merge their vectors.
The model keeps at most 128 cached embeddings, keyed by
hashes of the complete text, query flag and image bytes, to avoid repeated native
inference during retries without retaining image payloads.

The local Compose envelope defaults to 5 GiB without swap and three CPUs. Configure
`SCRIBE_MEMORY_LIMIT`, `SCRIBE_CPUS` and `SCRIBE_INFERENCE_THREADS` to fit the host;
BLAS and ONNX pools default to two threads to limit oversubscription. The envelope
protects the rest of the VM but cannot guarantee that every model combination
fits. Quantization quality, peak resident memory and latency require the live
model benchmark on the deployment architecture before changing the default.

Eru's `OpenInformationPipeline` supports open predicates for HippoRAG 2. A Registry
`OntologySlice` can accompany private extraction requests; pipeline blocks can also
use their existing task-bound Registry slice. The policy uses stable `link_type`
resource IDs and their `source_type`/`target_type` attributes. Endpoint types and
predicates are validated against that vocabulary after structured generation;
unlisted or ungrounded triples are rejected. An explicitly empty ontology remains
closed. Eru consumes authorized Registry projections and never substitutes a
locally invented ontology or treats extracted names as resolved ESKG identities.

The first local use downloads public weights; prewarm the cache for offline operation.
The embedding weights alone require approximately 4 GiB in BF16; provision additional
memory for loading, activations, extraction and reranking. Prewarm the new
`qwen3-vl` cache directory before offline indexing.
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

### Extraction credentials and retries

A provider HTTP 401 or 403 is published as `graph_extraction_unauthorized` alongside
`graph_extraction_unavailable`. Parsed passages and existing embeddings remain
searchable. The indexing job stops automatic retries and the library explains
that extraction credentials need attention. After correcting
`SCRIBE_OPENAI_API_KEY` (or the selected model's service credential), recreate
Scribe and use **Index** again; `indexDocument` reopens exhausted jobs.

Local entity proposals have a five-second wait budget and at most one outstanding
native operation per runtime. A failed or slow proposal does not prevent remote relation extraction:
the remote model may propose entities, and the same source grounding and Registry
constraints still validate its result. The warning `entity_extraction_skipped`
reports this fallback without scheduling redundant relation extraction.

Percentages retain their high-water mark across retries, while the stage tracks
the operation currently running. A retry therefore cannot remain labelled as
publishing merely because its previous attempt reached 95 percent.
