"""Small fixed HotpotQA distractor subset keeps inference out of regression runs."""

import hashlib
import json
import math
import random
import statistics
from pathlib import Path
from typing import Literal, TypedDict

from galadril_scribe.retrieval import fuse_ranks
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
from retrieval_benchmark import bm25_rank, cosine, recall_at


class PairedComparison(TypedDict):
    n: int
    mean_delta: float
    ci95: tuple[float, float]
    one_sided_p: float
    resamples: int


def paired_comparison(
    baseline: list[float],
    candidate: list[float],
    groups: list[str] | None = None,
) -> PairedComparison:
    """Use paired, stratified bootstrap and a one-sided sign-flip randomization test.

    The test assumes exchangeability under the null. It describes this sample;
    shared Wikipedia sources can induce dependence between question outcomes.
    """
    if len(baseline) != len(candidate) or len(baseline) < 30:
        raise ValueError(
            "Paired inference needs at least thirty matched questions"
        )
    if any(
        not math.isfinite(value) or not 0 <= value <= 1
        for value in (*baseline, *candidate)
    ):
        raise ValueError("Invalid paired retrieval score")
    labels = groups if groups is not None else ["all"] * len(baseline)
    if len(labels) != len(baseline):
        raise ValueError("Paired strata do not match question outcomes")
    differences = [
        right - left for left, right in zip(baseline, candidate, strict=True)
    ]
    strata: dict[str, list[float]] = {}
    for label, delta in zip(labels, differences, strict=True):
        strata.setdefault(label, []).append(delta)
    rng = random.Random(20261018)
    samples = 10000
    distribution = sorted(
        math.fsum(
            value
            for group in strata.values()
            for value in rng.choices(group, k=len(group))
        )
        / len(differences)
        for _ in range(samples)
    )
    quantiles = statistics.quantiles(distribution, n=40, method="inclusive")
    observed = math.fsum(differences)
    exceedances = sum(
        math.fsum(
            delta if rng.getrandbits(1) else -delta for delta in differences
        )
        >= observed - 1e-12
        for _ in range(samples)
    )
    return {
        "n": len(differences),
        "mean_delta": observed / len(differences),
        "ci95": (quantiles[0], quantiles[-1]),
        "one_sided_p": (exceedances + 1) / (samples + 1),
        "resamples": samples,
    }


class HotpotQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    question: str
    kind: Literal["bridge", "comparison"]
    context: dict[str, tuple[str, ...]] = Field(min_length=10, max_length=10)
    supporting_facts: tuple[tuple[str, int], ...]

    def texts(self) -> dict[str, str]:
        return {
            title: title + "\n" + "".join(sentences)
            for title, sentences in self.context.items()
        }


class HotpotCache(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_url: str
    source_sha256: str
    subset_sha256: str
    license: Literal["CC-BY-SA-4.0"]
    selection: str
    questions: tuple[HotpotQuestion, ...]
    embedding_profile: str
    vectors: dict[str, tuple[FiniteFloat, ...]]
    reranker_profile: str = ""
    reranking: dict[str, dict[str, FiniteFloat]] = Field(default_factory=dict)

    def vector(self, text: str, query: bool = False) -> tuple[float, ...]:
        return self.vectors[
            hashlib.sha256(f"{int(query)}:{text}".encode()).hexdigest()
        ]


def load_hotpot() -> HotpotCache:
    """Validate supporting sentence indices and immutable cached inference profiles."""
    data = HotpotCache.model_validate_json(
        (Path(__file__).parent / "fixtures" / "hotpotqa.json").read_bytes()
    )
    digest = hashlib.sha256(
        json.dumps(
            [item.model_dump() for item in data.questions],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if digest != data.subset_sha256 or len(
        {question.id for question in data.questions}
    ) != len(data.questions):
        raise ValueError("HotpotQA subset changed")
    for question in data.questions:
        data.vector(question.question, True)
        if set(data.reranking[question.id]) != set(question.context):
            raise ValueError("Incomplete HotpotQA reranking cache")
        for title, sentence in question.supporting_facts:
            if title not in question.context or not 0 <= sentence < len(
                question.context[title]
            ):
                raise ValueError("Invalid HotpotQA supporting sentence")
        for text in question.texts().values():
            data.vector(text)
    for vector in data.vectors.values():
        if len(vector) != 1024 or not math.isclose(
            math.fsum(value * value for value in vector), 1, abs_tol=1e-5
        ):
            raise ValueError("Invalid HotpotQA embedding")
    return data


class HotpotReport(TypedDict):
    queries: int
    recall_at_5: dict[str, float]
    both_supports_at_5: dict[str, float]
    results: dict[str, dict[str, list[str]]]
    primary_hypothesis: str
    paired_primary: PairedComparison
    recall_by_type: dict[str, dict[str, float]]


def evaluate_hotpot(data: HotpotCache) -> HotpotReport:
    """Rank candidates without consulting answers, question types, or support labels."""
    recalls: dict[str, list[float]] = {}
    joint: dict[str, list[float]] = {}
    selections: dict[str, dict[str, list[str]]] = {}
    for question in data.questions:
        texts = question.texts()
        lexical = bm25_rank(texts, question.question)
        semantic = sorted(
            texts,
            key=lambda title: (
                -cosine(
                    data.vector(question.question, True),
                    data.vector(texts[title]),
                ),
                title,
            ),
        )
        hybrid = [
            identifier for identifier, _ in fuse_ranks([lexical, semantic])
        ]
        reranked = sorted(
            hybrid,
            key=lambda title: (
                -data.reranking[question.id][title],
                hybrid.index(title),
            ),
        )
        selections[question.id] = {}
        for label, ranked in (
            ("lexical", lexical),
            ("semantic", semantic),
            ("hybrid", hybrid),
            ("hybrid_bge", reranked),
        ):
            selections[question.id][label] = ranked[:5]
            relevant = {title for title, _ in question.supporting_facts}
            recalls.setdefault(label, []).append(recall_at(ranked, relevant, 5))
            joint.setdefault(label, []).append(
                float(relevant <= set(ranked[:5]))
            )
    by_type = {}
    for kind in ("bridge", "comparison"):
        indices = [
            index
            for index, question in enumerate(data.questions)
            if question.kind == kind
        ]
        by_type[kind] = {
            label: statistics.mean(values[index] for index in indices)
            for label, values in recalls.items()
        }
    return {
        "queries": len(data.questions),
        "recall_at_5": {
            label: statistics.mean(values) for label, values in recalls.items()
        },
        "both_supports_at_5": {
            label: statistics.mean(values) for label, values in joint.items()
        },
        "results": selections,
        "primary_hypothesis": "H0: hybrid_bge does not improve Recall@5 over semantic; H1: paired mean improvement > 0; alpha=0.05",
        "paired_primary": paired_comparison(
            recalls["semantic"],
            recalls["hybrid_bge"],
            [question.kind for question in data.questions],
        ),
        "recall_by_type": by_type,
    }
