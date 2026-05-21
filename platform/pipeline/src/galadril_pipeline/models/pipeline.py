from pydantic import BaseModel
from typing import List, Optional, Literal


class ColumnMapping(BaseModel):
    column_name: str
    data: Optional[str] = None


class StepParams(BaseModel):
    threshold: Optional[float] = None
    table_name: Optional[str] = None
    columns: Optional[List[ColumnMapping]] = None
    limit: Optional[int] = None
    on_no_match: Optional[str] = None

    trigger: Optional[Literal["cron", "on_step_completed", "manual"]] = None
    cron: Optional[str] = None
    on_step: Optional[str] = None

    target: Optional[str] = None
    lookback: Optional[str] = None
    k_hops: Optional[int] = None
    max_vertices: Optional[int] = None
    max_events: Optional[int] = None
    max_states: Optional[int] = None
    max_embeddings_per_entity: Optional[int] = None

    amarth_target_outcome: Optional[str] = None
    amarth_time_col: Optional[str] = None
    amarth_embedding_col: Optional[str] = None
    amarth_window_size: Optional[str] = None

    # Allow dynamic parameters.
    model_config = {"extra": "allow"}


class PipelineStep(BaseModel):
    step: str
    type: str
    connector: Optional[str] = None
    model: Optional[str] = None
    artifact_path: Optional[str] = None
    input_from: List[str]
    params: Optional[StepParams] = None
