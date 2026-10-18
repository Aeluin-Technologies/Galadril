# Cached document retrieval measurements

The corpus derives from the attributed [UCI Online Retail dataset](https://archive.ics.uci.edu/dataset/352/online+retail),
Daqing Chen, CC BY 4.0. `retail.csv` is the same 240-row observation fixture ingested
by the ESKG lifecycle test. `retrieval.json` retains its SHA-256, customer IDs and
source-row IDs. Eight selected customers each provide a product, invoice and
customer passage; each query has three independently specified relevant passages.

The fixture contains real local model outputs, not hand-written vectors: Qwen3
Embedding 0.6B Q8, BGE v2-m3 ONNX int8, Qwen3 Reranker 0.6B Q8, and Eru's local
Qwen3 0.6B Q8 knowledge projection. Each profile fingerprints weights and inference
policy. Ordinary Bazel tests replay these outputs without loading weights or using
an inference API. No API key is needed.

Run `//tests/e2e:retrieval_benchmark_test` for ranking and extraction measurements.

## Full official engine with ESKG

`retrieval_official.json` adds 32 fixed questions over the same 24 source-grounded
passages. Each of the eight selected observations supplies four predefined tasks:
product-to-country, customer-to-product/invoice, invoice-date/customer-country, and
invoice-product/price. The selected product and invoice identities are unique in
this candidate corpus. No questions are removed after scoring.

Qwen query vectors and BGE query/passage scores were computed locally once. Facts
remain controlled triples derived from the original source rows; recognition uses
Eru's deterministic named-anchor policy. This benchmark measures retrieval while
holding extraction fixed, rather than attributing perfect extraction to Eru.

`//tests/e2e:official_memory_test` runs the complete pinned upstream index/retrieve
engine, comparing dense, official, official+ESKG and both graph variants with local
reranking and reciprocal rank fusion. The offline bridge uses source-fixture
identities. The live lifecycle supplies actual entity/event IDs, checks current
document/graph permissions through GraphQL and preserves ESKG state/vector counts.
Its setup attaches the derived documents to their source observations explicitly;
it does not claim a new chatbot ingestion or pipeline-authoring endpoint.

Recall@5 is 0.3177 (dense), 0.8229 (official), 0.8438 (official+ESKG), and 0.8333
for both variants after reranking/fusion. The raw ESKG gain does not survive the
final fusion on this fixture. Thirty-two questions sharing eight source clusters
are not thirty-two independent observations. These are regression measurements,
not evidence of normality, statistical significance or general legal quality.
The separate 40-question HotpotQA benchmark retains its own protocol and scope.

Regenerate deliberately with compatible local model profiles:

```sh
PYTHONPATH=tests/e2e uv run python tests/e2e/fixtures/build_official_memory_fixture.py --cache /path/to/local-models --output tests/e2e/fixtures/retrieval_official.json
```

Normal tests replay the cached computations and never download model weights or
call an inference provider. Attribution and license follow the UCI source above.
The full `//tests/e2e:pipeline_lifecycle_test` additionally runs real uploads,
PostgreSQL BM25/vector queries, GraphQL authorization, scoped graph expansion and
unchanged ESKG state/vector counts. Its `retrieval-benchmark.json` artifact records
Recall@5 and selected sources for all four search modes.

The retrieval benchmark uses curated source-derived triples to isolate ranking.
The separate Eru cache measures actual extraction against annotated endpoint pairs
(predicate synonyms are not scored). Source-grounded mentions alone do not prove
semantic entailment. The retained first-pass extraction cache records the earlier
measurement before the direct local fallback, rather than a second gold graph.
These are small regression fixtures, not a legal-domain or general model leaderboard.

To regenerate expensive outputs deliberately, use a uv environment containing the
workspace packages and run from the repository root. Set `PYTHONPATH` to
`machine-learning/scribe/python:machine-learning/eru/src:machine-learning/galadril-inference/src:platform/pipeline/src:tests/e2e`
when running without installed workspace packages. Use one persistent model cache:

```sh
uv run python tests/e2e/fixtures/build_retrieval_fixture.py --cache /path/to/models
uv run python tests/e2e/fixtures/build_reranker_fixture.py --model-cache /path/to/models --backend bge
uv run python tests/e2e/fixtures/build_reranker_fixture.py --model-cache /path/to/models --backend qwen
uv run python tests/e2e/fixtures/build_eru_fixture.py --model-cache /path/to/models
```

The first command recomputes embeddings. The other commands reuse them. Run each
reranker command in a fresh process: peak RSS is process-wide and the first timed
query includes model startup (and download on an empty cache). Subsequent timings
measure a warm model. The recorded measurements used native CPU execution on
macOS arm64, with different batching strategies for ONNX and llama.cpp; comparisons
are specific to those adapters and these short passages.

Eru progress files avoid redoing completed passages during manual generation. Remove
that local progress file when changing the extraction policy or model artifact.
Always review fixture changes and rerun the source-consistency tests before commit.

Additional offline targets audit algorithm identity and chunking:

- `//tests/e2e:upstream_retrieval_test` reuses the pinned official graph methods,
  including synonym edges and frequency-weighted seeds. It compares the graph
  alone with source-verified ESKG customer identity links, holding model outputs
  fixed. Recall@5 on the retail fixture is 0.8333 versus 0.875. This is a graph-kernel
  ablation, not full upstream inference/persistence parity.
- `//tests/e2e:chunking_benchmark_test` compares 256-character fixed and structural
  boundaries with 32-character overlap, plus experimental contextual causal mean
  pooling. Its qrels remain the original source passages; a hit covers at least half
  a passage, counted once. Source-evidence Recall@5 is 0.8333, 0.7917 and 0.625,
  respectively. These chunk-level results are not directly comparable with the
  passage-ranking table. Neither semantic adaptive chunking nor paper-equivalent
  bidirectional Late Chunking is demonstrated.
- `//tests/e2e:hotpot_benchmark_test` uses 40 fixed HotpotQA distractor questions
  with cached local embeddings/reranking and a predefined paired hypothesis. See
  [HOTPOTQA.md](HOTPOTQA.md) for selection, attribution and statistical limitations.

`build_phrase_fixture.py` regenerates the normalized phrase vectors for the
upstream reference. `build_chunking_fixture.py` uses separate `pooled` and
`contextual` stages so native contexts do not accumulate in one process. Both take
`--cache` and `--output`. The contextual profile is experimental Qwen causal pooling,
not interchangeable with its trained last-token profile.
