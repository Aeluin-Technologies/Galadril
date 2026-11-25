"""Gateway publication against Registry and immutable GOL bindings."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest
from assertions import (
    eventually,
    require_mapping,
    require_sequence,
)
from authentication import login_user
from clients import (
    ONTOLOGY_ID,
    PIPELINE_ID,
    TENANT_ID,
    UPLOADER_ID,
    VISION_RUNTIME_READY_TIMEOUT_SECONDS,
    GatewayClient,
    RegistryFixtures,
    vision_runtime_ready,
)
from gateway_scenario import GatewayScenario, _field, _string
from gol_authoring import gol_access, publish_gol_source
from pipeline_definition import pipeline_fixture

AuthoringReview = Callable[[GatewayClient, str, str], Awaitable[None]]


def _ontology() -> str:
    """Declares canonical types without treating graph labels as ontology IDs."""
    return f"""ontology {ONTOLOGY_ID} version "1.0.0" {{
    entity E2EEntity {{ field display_name: String; }}
    entity E2EObservation {{ field kind: String; }}
    entity ObservationEdge {{
        field tenant_id: String;
        field source_id: String;
        field target_id: String;
        field edge_type: String;
        field modality: String;
        field raw_modality: Option<String>;
        field confidence: Option<f64>;
        field resolution_probability: Option<f64>;
        field licorne_decision_id: Option<u64>;
    }}
    entity ObservationState {{
        field tenant_id: String;
        field entity_id: String;
        field event_id: String;
        field state_type: String;
        field event_time: String;
        field modality: String;
        field model_name: String;
        field confidence: Option<f64>;
        field bbox: Option<List<f64>>;
        field display_label: Option<String>;
        field model_version: Option<String>;
        field mime_type: Option<String>;
        field raw_modality: Option<String>;
        field source_field: Option<String>;
        field is_unknown: Option<bool>;
        field resolution_action: Option<String>;
        field resolution_probability: Option<f64>;
        field resolution_labels: Option<List<String>>;
        field resolution_scores: Option<List<f64>>;
        field licorne_identity_id: Option<u64>;
        field licorne_observation_id: Option<u64>;
        field licorne_decision_id: Option<u64>;
        field licorne_inference_id: Option<u64>;
        field licorne_version: Option<u64>;
        field licorne_created_identity: Option<bool>;
        field licorne_iterations: Option<u64>;
        field licorne_residual: Option<f64>;
        field licorne_exact: Option<bool>;
        field h3_cell: Option<u64>;
        field lat: Option<f64>;
        field lon: Option<f64>;
        field accuracy_meters: Option<f64>;
        field scalar_names: Option<List<String>>;
        field scalar_values: Option<List<f64>>;
    }}
    entity ObservationEmbedding {{
        field tenant_id: String;
        field entity_id: String;
        field event_id: String;
        field timestamp: String;
        field state_type: String;
        field entity_type: String;
        field modality: String;
        field raw_modality: Option<String>;
        field model_name: String;
        field model_version: Option<String>;
        field licorne_identity_id: Option<u64>;
        field licorne_version: Option<u64>;
        field embedding: List<f64>;
    }}
    entity IdentityPrototype {{
        field entity_id: String;
        field licorne_identity_id: String;
        field modality: String;
        field embedding: List<f64>;
        field observation_key: String;
        field latitude: Option<f64>;
        field longitude: Option<f64>;
        field accuracy_meters: f64;
    }}
    entity CausalRun {{
        field cache_key: String;
        field target: String;
        field window_start: String;
        field window_end: String;
        field status: String;
        field error: Option<String>;
        field reason: Option<String>;
        field persisted_edges: Option<u32>;
        field causal_links: Option<u32>;
        field validated_effects: Option<u32>;
        field entity_scope_size: Option<u32>;
        field event_scope_size: Option<u32>;
        field observation_count: Option<u32>;
        field relationship_count: Option<u32>;
        field counterfactual_ready: Option<bool>;
        field effect_sizes: List<f64>;
        field confidence_scores: List<f64>;
        field lag_seconds: List<f64>;
        field p_values: List<Option<f64>>;
        field q_values: List<Option<f64>>;
        field stabilities: List<f64>;
        field counterfactual_flags: List<bool>;
    }}
    entity CausalLink {{
        field inference_id: String;
        field source_feature: String;
        field target_feature: String;
        field confidence_score: f64;
        field time_lag_seconds: f64;
        field lag_steps: u32;
        field effect_size: f64;
        field p_value: Option<f64>;
        field q_value: Option<f64>;
        field stability: f64;
        field method: String;
        field window_start: String;
        field window_end: String;
        field bucket_seconds: f64;
        field target: String;
        field samples_processed: u32;
        field source_node_ids: List<String>;
        field target_node_ids: List<String>;
        field supports_counterfactual: bool;
        field counterfactual_model: String;
        field updated_at: String;
        field average_treatment_effect: Option<f64>;
        field refutation_passed: Option<bool>;
    }}
    {gol_access("E2EEntity")}
    {gol_access("E2EObservation")}
    {gol_access("ObservationEdge")}
    {gol_access("ObservationState")}
    {gol_access("ObservationEmbedding")}
    {gol_access("IdentityPrototype")}
    {gol_access("CausalRun")}
    {gol_access("CausalLink")}
}}"""


def _pipeline() -> dict[str, object]:
    """Returns the shared deterministic runtime DAG."""
    return pipeline_fixture(TENANT_ID, PIPELINE_ID)


async def provision_pipeline(
    scenario: GatewayScenario, *, review: AuthoringReview | None = None
) -> str:
    """Deploys the same immutable pipeline revision for every streaming scenario."""
    gateway = scenario.gateway
    spicedb = scenario.spicedb
    uploader_token = scenario.uploader_token
    outsider_token = scenario.outsider_token
    print("E2E stage: publishing ontology", flush=True)
    await publish_gol_source(gateway, uploader_token, ONTOLOGY_ID, _ontology())
    if review is not None:
        await review(gateway, uploader_token, outsider_token)
    print("E2E stage: publishing pipeline", flush=True)
    pipeline_data = await gateway.execute(
        uploader_token,
        """
        mutation CreatePipeline(
          $pipelineId: String!,
          $name: String!,
          $definition: JSON!,
          $message: String!
        ) {
          createPipeline(
            pipelineId: $pipelineId,
            name: $name,
            definition: $definition,
            message: $message
          ) { pipelineId headRevisionId ownerId }
        }
        """,
        {
            "pipelineId": PIPELINE_ID,
            "name": PIPELINE_ID,
            "definition": json.dumps(
                _pipeline(), separators=(",", ":"), sort_keys=True
            ),
            "message": "Create deterministic E2E pipeline",
        },
    )
    created_pipeline = _field(pipeline_data, "createPipeline")
    head_revision = _string(created_pipeline, "headRevisionId")
    assert created_pipeline.get("ownerId") == UPLOADER_ID

    if review is not None:
        assert await spicedb.allowed(
            resource_type="pipeline",
            resource_id=f"{TENANT_ID}/{PIPELINE_ID}",
            permission="publish",
            user_id=UPLOADER_ID,
        )
        definitions = await gateway.execute(
            uploader_token,
            """
            query Pipelines {
              pipelineDefinitions { pipelineId headRevisionId }
            }
            """,
        )
        pipelines = require_sequence(
            definitions.get("pipelineDefinitions"), "pipelineDefinitions"
        )
        assert any(
            require_mapping(item, "pipeline definition").get("pipelineId")
            == PIPELINE_ID
            for item in pipelines
        )
        revised_data = await gateway.execute(
            uploader_token,
            """
            mutation UpdatePipeline(
              $pipelineId: String!,
              $revisionId: String!,
              $definition: JSON!
            ) {
              updatePipeline(
                pipelineId: $pipelineId,
                expectedHeadRevisionId: $revisionId,
                name: "e2e_pipeline",
                definition: $definition,
                message: "Revise deterministic E2E pipeline"
              ) { headRevisionId }
            }
            """,
            {
                "pipelineId": PIPELINE_ID,
                "revisionId": head_revision,
                "definition": json.dumps(
                    _pipeline(), separators=(",", ":"), sort_keys=True
                ),
            },
        )
        head_revision = _string(
            _field(revised_data, "updatePipeline"), "headRevisionId"
        )
        with pytest.raises(AssertionError, match="GraphQL operation failed"):
            await gateway.execute(
                outsider_token,
                """
                mutation PublishPipeline(
                  $pipelineId: String!, $revisionId: String!
                ) {
                  publishPipeline(
                    pipelineId: $pipelineId,
                    revisionId: $revisionId
                  ) { pipelineId }
                }
                """,
                {
                    "pipelineId": PIPELINE_ID,
                    "revisionId": head_revision,
                },
            )
    for block_id in (
        "infer",
        "resolve",
        "sink",
        "causal",
        "empirical_infer",
        "empirical_resolve",
        "empirical_sink",
    ):
        bound = await gateway.execute(
            uploader_token,
            "mutation($ontology:String!,$pipeline:String!,$block:String!,$head:String!){bindGolOntology(ontologyId:$ontology,pipelineId:$pipeline,blockId:$block,expectedPipelineRevision:$head)}",
            {
                "ontology": ONTOLOGY_ID,
                "pipeline": PIPELINE_ID,
                "block": block_id,
                "head": head_revision,
            },
        )
        head_revision = _string(bound, "bindGolOntology")

    # Binding GOL puts the pipeline under Registry's authoring workflow.
    for operation in ("approve", "publish", "deploy"):
        published_data = await gateway.execute(
            uploader_token,
            """
            mutation Author($id:String!,$definition:JSON!,$command:JSON!,$head:String!) {
              authorPipeline(pipelineId:$id,name:$id,definition:$definition,
                command:$command,expectedHeadRevisionId:$head,
                message:"Deploy deterministic GOL pipeline") {
                pipelineId headRevisionId publishedRevisionId lifecycle
              }
            }
            """,
            {
                "id": PIPELINE_ID,
                "definition": json.dumps(_pipeline()),
                "command": json.dumps({"operation": operation}),
                "head": head_revision,
            },
        )
        published_pipeline = _field(published_data, "authorPipeline")
        head_revision = _string(published_pipeline, "headRevisionId")
    assert published_pipeline.get("lifecycle") == "deployed"
    published_revision = _string(published_pipeline, "publishedRevisionId")
    assert head_revision != published_revision
    registry = RegistryFixtures()
    try:
        runtime_definition = await registry.get_runtime_pipeline(
            published_revision
        )
    finally:
        await registry.close()
    expected_runtime = _pipeline()
    for step in require_sequence(expected_runtime["pipeline"], "steps"):
        require_mapping(step, "bound step")["ontology_required"] = True
    assert json.loads(runtime_definition) == expected_runtime
    return head_revision


async def start_streaming_pipeline(
    scenario: GatewayScenario, *, review: AuthoringReview | None = None
) -> str:
    """Proves cold-start publication pickup before accepting uploads."""
    environment = scenario.environment
    await environment.start_vision()
    await eventually(
        environment.vision_waiting_for_publication,
        timeout_seconds=VISION_RUNTIME_READY_TIMEOUT_SECONDS,
        description="Vision waiting for its unpublished tenant pipeline",
        abort_on=(AssertionError,),
    )
    head_revision = await provision_pipeline(scenario, review=review)
    await eventually(
        vision_runtime_ready,
        timeout_seconds=VISION_RUNTIME_READY_TIMEOUT_SECONDS,
        description="Vision database and Kafka consumers",
    )
    await environment.assert_vision_did_not_restart()
    return head_revision


async def retire_pipeline(
    scenario: GatewayScenario, head_revision: str
) -> None:
    """Retires the published pipeline and its ontology through public operations."""
    gateway = scenario.gateway
    uploader_token = await login_user(UPLOADER_ID)
    print("E2E stage: retiring Registry resources", flush=True)
    deleted_pipeline = await gateway.execute(
        uploader_token,
        """
        mutation DeletePipeline(
          $pipelineId: String!, $revisionId: String!
        ) {
          deletePipeline(
            pipelineId: $pipelineId,
            expectedHeadRevisionId: $revisionId
          )
        }
        """,
        {
            "pipelineId": PIPELINE_ID,
            "revisionId": head_revision,
        },
    )
    assert deleted_pipeline.get("deletePipeline") is True
    workspace = await gateway.execute(
        uploader_token,
        "query($id:String!){golOntologyWorkspace(ontologyId:$id){summary{sourceRevision}}}",
        {"id": ONTOLOGY_ID},
    )
    expected = _string(
        _field(_field(workspace, "golOntologyWorkspace"), "summary"),
        "sourceRevision",
    )
    retired_ontology = await gateway.execute(
        uploader_token,
        "mutation($id:String!,$expected:String!){deleteGolOntology(ontologyId:$id,expectedRevision:$expected){revision}}",
        {"id": ONTOLOGY_ID, "expected": expected},
    )
    assert _string(_field(retired_ontology, "deleteGolOntology"), "revision")
