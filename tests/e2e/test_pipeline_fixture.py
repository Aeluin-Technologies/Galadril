"""Checks the real E2E DAG without importing model or Docker runtimes."""

import pytest
from galadril_pipeline.config import PipelineConfig
from galadril_pipeline.routing import PipelineRouteTable
from pipeline_definition import pipeline_fixture


def test_empirical_pipeline_routes_are_streaming_safe() -> None:
    """Both sources compile without an implicit streaming join."""
    routes = PipelineRouteTable(
        PipelineConfig.model_validate(pipeline_fixture("tenant", "pipeline"))
    )
    assert routes.entry_steps("e2e_text") == ("infer",)
    assert routes.entry_steps("e2e_empirical") == ("empirical_infer",)
    assert routes.route("empirical_resolve").dependencies == (
        "empirical_infer",
    )


def test_every_pipeline_publication_names_its_bound_gol_contract() -> None:
    """Kafka dispatch cannot infer a transfer contract from a model name."""
    config = PipelineConfig.model_validate(
        pipeline_fixture("tenant", "pipeline")
    )
    for step in config.pipeline:
        assert step.params.model_dump().get("gol_transfer_symbol_key") in {
            "e2e.lifecycle::E2EEntity",
            "e2e.lifecycle::E2EObservation",
        }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
