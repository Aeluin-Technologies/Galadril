"""Typed ESKG observation-window contracts and zero-copy-aware preparation."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator


class GraphRelationshipObservation(BaseModel):
    """Represents ontology or analytically derived graph evidence."""

    model_config = ConfigDict(frozen=True)

    source_node_id: str
    target_node_id: str
    relationship_type: str
    observed_at: datetime
    properties: dict[str, str | float | int | bool | None] = Field(
        default_factory=dict
    )


class Observation(BaseModel):
    """Represents one heterogeneous observation backed by an ESKG node."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    graph_node_id: str
    observed_at: datetime
    observation_type: str
    scalar_values: dict[str, float] = Field(default_factory=dict)
    embeddings: dict[str, tuple[float, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_embedding_dimensions(self) -> Observation:
        """Rejects empty vectors because their feature shape is undefined."""
        if any(not isfinite(value) for value in self.scalar_values.values()):
            raise ValueError("scalar observation values must be finite")
        for name, vector in self.embeddings.items():
            if not vector:
                raise ValueError(f"embedding '{name}' must not be empty")
            if any(not isfinite(value) for value in vector):
                raise ValueError(
                    f"embedding '{name}' must contain finite values"
                )
        return self


class ObservationWindow(BaseModel):
    """Defines an immutable, bounded ESKG evidence window."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    start: datetime
    end: datetime
    bucket: timedelta
    observations: tuple[Observation, ...]
    relationships: tuple[GraphRelationshipObservation, ...] = ()

    @model_validator(mode="after")
    def validate_bounds(self) -> ObservationWindow:
        """Prevents temporal leakage and invalid sampling intervals."""
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError(
                "observation window timestamps must be timezone-aware"
            )
        if self.end <= self.start:
            raise ValueError("observation window end must follow start")
        if self.bucket.total_seconds() <= 0.0:
            raise ValueError("observation window bucket must be positive")
        for observation in self.observations:
            if not self.start <= observation.observed_at <= self.end:
                raise ValueError(
                    f"observation '{observation.observation_id}' is outside "
                    "the observation window"
                )
        for relationship in self.relationships:
            if not self.start <= relationship.observed_at <= self.end:
                raise ValueError(
                    f"relationship '{relationship.relationship_type}' is "
                    "outside the observation window"
                )
        return self


class PreparedObservationWindow(BaseModel):
    """Contains the aligned numerical input and ESKG provenance index."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    frame: pd.DataFrame
    vector_columns: tuple[str, ...]
    feature_node_ids: dict[str, tuple[str, ...]]
    sampling_interval_seconds: float


class CausalLink(BaseModel):
    """Represents a persistable, provenance-aware directional causal link."""

    model_config = ConfigDict(frozen=True)

    source_feature: str
    target_feature: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    time_lag_seconds: float = Field(ge=0.0)
    lag_steps: int = Field(ge=0)
    effect_size: float
    p_value: float | None = Field(default=None, ge=0.0, le=1.0)
    q_value: float | None = Field(default=None, ge=0.0, le=1.0)
    stability: float = Field(ge=0.0, le=1.0)
    method: str
    source_node_ids: tuple[str, ...] = ()
    target_node_ids: tuple[str, ...] = ()
    supports_counterfactual: bool = True


def _bucket_index(
    observed_at: datetime, start: datetime, bucket_seconds: float
) -> int:
    """Maps an in-window timestamp to its preallocated row."""
    return int((observed_at - start).total_seconds() // bucket_seconds)


def prepare_observation_window(
    window: ObservationWindow,
) -> PreparedObservationWindow:
    """Aligns heterogeneous ESKG evidence without unpacking dense vectors."""
    bucket_seconds = window.bucket.total_seconds()
    bucket_count = (
        int((window.end - window.start).total_seconds() // bucket_seconds) + 1
    )
    timestamps = [
        window.start + (window.bucket * index) for index in range(bucket_count)
    ]

    scalar_accumulators: dict[tuple[str, int], list[float]] = {}
    vector_accumulators: dict[tuple[str, int], list[np.ndarray]] = {}
    feature_nodes: dict[str, set[str]] = {}

    for observation in window.observations:
        row = _bucket_index(
            observation.observed_at, window.start, bucket_seconds
        )
        feature_nodes.setdefault(observation.observation_type, set()).add(
            observation.graph_node_id
        )
        for name, value in observation.scalar_values.items():
            column = f"{observation.observation_type}.{name}"
            scalar_accumulators.setdefault((column, row), []).append(value)
        for name, vector in observation.embeddings.items():
            column = f"{observation.observation_type}.{name}"
            vector_accumulators.setdefault((column, row), []).append(
                np.asarray(vector, dtype=np.float64)
            )

    columns: dict[str, np.ndarray] = {}
    for column, _ in scalar_accumulators:
        columns.setdefault(column, np.zeros(bucket_count, dtype=np.float64))
    for (column, row), values in scalar_accumulators.items():
        columns[column][row] = float(np.mean(values))

    vector_columns = tuple(sorted({key[0] for key in vector_accumulators}))
    vector_data: dict[str, np.ndarray] = {}
    for column in vector_columns:
        column_vectors = [
            vector
            for (feature, _), vectors in vector_accumulators.items()
            if feature == column
            for vector in vectors
        ]
        dimensions = {vector.shape for vector in column_vectors}
        if len(dimensions) != 1:
            raise ValueError(f"embedding '{column}' changed dimensions")
        dimension = column_vectors[0].shape[0]
        matrix = np.zeros((bucket_count, dimension), dtype=np.float64)
        data = np.empty(bucket_count, dtype=object)
        for row in range(bucket_count):
            data[row] = matrix[row]
        vector_data[column] = data
    for (column, row), vectors in vector_accumulators.items():
        dimensions = {vector.shape for vector in vectors}
        if len(dimensions) != 1:
            raise ValueError(
                f"embedding '{column}' changed dimensions within one bucket"
            )
        vector_data[column][row][:] = np.mean(vectors, axis=0)

    relationship_columns: dict[str, np.ndarray] = {}
    for relationship in window.relationships:
        column = f"relationship.{relationship.relationship_type.upper()}"
        data = relationship_columns.setdefault(
            column, np.zeros(bucket_count, dtype=np.float64)
        )
        row = _bucket_index(
            relationship.observed_at, window.start, bucket_seconds
        )
        data[row] += 1.0

    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            **columns,
            **vector_data,
            **relationship_columns,
        }
    )
    return PreparedObservationWindow(
        frame=frame,
        vector_columns=vector_columns,
        feature_node_ids={
            feature: tuple(sorted(node_ids))
            for feature, node_ids in feature_nodes.items()
        },
        sampling_interval_seconds=bucket_seconds,
    )
