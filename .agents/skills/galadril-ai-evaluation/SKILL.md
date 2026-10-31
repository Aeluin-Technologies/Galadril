---
name: galadril-ai-evaluation
description: >-
  Use when evaluating changes to Galadril's Scribe RAG, Eru extraction,
  inference models, embeddings, ranking, entity resolution or causal ML
  workflows. Not for infrastructure-only or ordinary model API wiring.
---
# Reproducible ML and retrieval evaluation

1. Identify the affected inference or retrieval contract and intended user
   outcome. Inspect actual code, fixtures and available evaluation harnesses
   before selecting metrics or adding dependencies.
2. Version the evaluation inputs, model/weights, prompt where relevant,
   runtime configuration and immutable Registry/ontology references. Separate
   development data from a held-out evaluation set; avoid source/tenant
   leakage through examples, cached embeddings and indexes.
3. Choose task-specific metrics and an explicit baseline: extraction schema
   validity and precision/recall, retrieval Recall@k and provenance accuracy,
   entity-resolution error, ranking metrics, or calibration of causal claims.
   Do not infer causal validity from predictive accuracy alone.
4. Include authorized and unauthorized retrieval cases, empty and ambiguous
   evidence, stale source revisions, prompt injection in retrieved material,
   timeout and partial-service failure. A model suggestion does not authorize
   graph mutation, tool invocation or cross-tenant evidence access.
5. Evaluate quality jointly with p95/p99, memory/GPU footprint, timeout rate
   and cost where measured. Detect regression by slice, not only an aggregate.
6. Exercise deterministic Bazel tests and the actual evaluation harness.
   Provide paired examples and uncertainty; do not claim statistical
   significance without sufficient design or observations.

Deliver: dataset/provenance, baseline, metrics, failure slices, latency/cost,
reproducibility commands and residual gaps. Never place raw sensitive tenant
documents, prompts or credentials in shared evaluation reports.
