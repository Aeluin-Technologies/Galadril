# HotpotQA retrieval smoke benchmark

`hotpotqa.json` contains a modified subset of **HotpotQA**, Zhilin Yang, Peng Qi,
Saizheng Zhang, Yoshua Bengio, William W. Cohen, Ruslan Salakhutdinov and Christopher
D. Manning, EMNLP 2018. Dataset and derived fixture: **CC BY-SA 4.0**.
See the [dataset homepage](https://hotpotqa.github.io/),
[paper](https://arxiv.org/abs/1809.09600), and
[license](https://creativecommons.org/licenses/by-sa/4.0/).

The source is the HotpotQA reproduction dataset distributed by upstream HippoRAG,
fixed at commit `d5c8329422e0a0b834a15874545cb6a74b4f9b26`. The fixture records its
source URL and SHA-256, the selected question IDs, original context sentences and
supporting sentence annotations, and a SHA-256 of the selected question payloads.
Answer strings are omitted. Text sent to embedding/reranking models contains no
supporting annotations, question type, answer or relevance label.

Selection is fixed before measuring scores: choose the first **20 hard bridge and
20 hard comparison** questions in ascending SHA-256(question ID), requiring the ten
contexts of the distractor protocol. The upstream reproduction file has some rows
with fewer contexts; this structural eligibility rule does not inspect retrieval
quality. No failures or low-scoring examples are removed after measurement.

Each question ranks its ten supplied paragraphs. Recall@5 measures recovery of the
two annotated supporting documents; `both_supports_at_5` additionally requires both
in the top five. This is paragraph retrieval, not the official answer/sentence F1,
not a full-Wikipedia benchmark, and not a leaderboard result. The balanced hard
sample is not weighted like the complete HotpotQA population.

The primary comparison is specified as `hybrid_bge - semantic` Recall@5, one-sided
alpha 0.05. The report retains per-question results, type-specific means, the paired
mean difference, a stratified paired percentile-bootstrap 95% interval and a paired
sign-flip Monte Carlo p-value. Both use 10,000 replicates and a fixed seed. The
randomization test assumes exchangeability under the null; question-level resampling
can underestimate uncertainty when questions share Wikipedia sources. Other modes
are descriptive comparisons, not additional unadjusted significance claims. Merely
having at least 30 observations does not establish normality or independence.

The cached measurement reports Recall@5 of 0.8000 (BM25), 0.8875 (dense), 0.8875
(hybrid) and 0.9875 (hybrid + BGE). Both supporting documents are found in 0.9750 of
questions after reranking. The primary paired gain is 0.1000, its bootstrap interval
is [0.0500, 0.1625], and the fixed Monte Carlo p-value is 0.00459954. These are
exploratory results for this balanced hard sample. All ten distractor paragraphs
are candidates for reranking; this does not measure first-stage candidate loss on
a large corpus. Regression floors retain these measured baselines.

Qwen3 Embedding 0.6B Q8 vectors and BGE v2-m3 ONNX int8 scores are actual local model
outputs with immutable profiles. `//tests/e2e:hotpot_benchmark_test` replays the cache
without network access, weights or model execution. Ordinary tests also run the
statistical calculations, including matched-pair and zero-variance regressions.

To regenerate deliberately, obtain the source from the pinned upstream commit, use
the workspace packages in a uv environment, and run:

```sh
uv run python tests/e2e/fixtures/build_hotpot_fixture.py --source /path/to/hotpotqa.json --output tests/e2e/fixtures/hotpotqa.json --cache /path/to/qwen --stage embed
uv run python tests/e2e/fixtures/build_hotpot_fixture.py --source /path/to/hotpotqa.json --output tests/e2e/fixtures/hotpotqa.json --cache /path/to/bge --stage rerank
```

The generator reuses compatible cached computations and rejects profile/source
changes. To change a model policy, regenerate in a fresh output file. Download and
inference happen only during this explicit regeneration. The live ESKG integration
continues to use the separate, source-coherent retail fixture. HotpotQA is not
claimed to have been ingested into ESKG or evaluated through the full official
HippoRAG OpenIE/recognition pipeline.
