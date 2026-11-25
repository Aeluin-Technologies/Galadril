"""Live public GraphQL query inventory against persisted pipeline data."""

from __future__ import annotations

from assertions import (
    require_mapping,
    require_sequence,
)
from clients import (
    ONTOLOGY_ID,
    PIPELINE_ID,
    GatewayClient,
)
from gateway_scenario import _field


async def exercise_query_api(
    gateway: GatewayClient, token: str, entity_id: str
) -> None:
    """Executes every available GraphQL Query field against live services."""
    data = await gateway.execute(
        token,
        """
        query PublicQueries(
          $entityId: String!, $pipelineId: String!
        ) {
          searchEntities(query: "gateway", limit: 10) {
            entityId metadata
          }
          globalSearch(query: "gateway", limit: 10) {
            kind entityId eventId
          }
          structuredSearch(
            input: {entityId: $entityId, limit: 10}
          ) { kind entityId }
          structuredSearchPage(input: {text: "gateway", limit: 1}) {
            items { kind entityId eventId } after
          }
          searchEvents(text: "gateway", limit: 10) { eventId eventType }
          searchEmbeddings(queryText: "gateway", k: 10) { entityId modality score }
          entityRelations(entityId: $entityId, depth: 2, limit: 10) {
            nodes { id label }
            edges { fromId toId label }
          }
          administration {
          users(limit: 10) { items { userId isActive } after }
          roles(limit: 10) { items { roleName } after }
          roleAssignments(limit: 10) { userId roleName }
          auditEvents(limit: 100) {
            action outcome requestId traceId revisionId publicationId
          }
          }
          golOntologies(limit: 10) {
            ontologyId activeGeneration sourceRevision
          }
          ontologyBindings(pipelineId: $pipelineId, limit: 10) {
            pipelineId blockId ontologyId
          }
          pipelineExecutions(pipelineId: $pipelineId, limit: 100) {
            pipelineId step status correlationId
          }
          conversations(limit: 10) { conversationId }
          scribeModels {
            defaultModel
            models { alias provider model reasoningEfforts defaultReasoningEffort }
          }
          pipelineDefinitions(limit: 10) {
            pipelineId headRevisionId publishedRevisionId
          }
        }
        """,
        {"entityId": entity_id, "pipelineId": PIPELINE_ID},
    )
    sequence_fields = (
        "searchEntities",
        "globalSearch",
        "structuredSearch",
        "searchEvents",
        "searchEmbeddings",
        "golOntologies",
        "ontologyBindings",
        "pipelineExecutions",
        "conversations",
        "pipelineDefinitions",
    )
    for field in sequence_fields:
        require_sequence(data.get(field), field)
    for field in ("users", "roles"):
        page = require_mapping(_field(data, "administration").get(field), field)
        require_sequence(page.get("items"), f"{field} items")
    for field in ("roleAssignments", "auditEvents"):
        require_sequence(_field(data, "administration").get(field), field)
    search_page = _field(data, "structuredSearchPage")
    assert (
        len(require_sequence(search_page.get("items"), "search page items"))
        <= 1
    )
    cursor = search_page.get("after")
    if isinstance(cursor, str):
        continuation = await gateway.execute(
            token,
            """query($after: String!) {
              structuredSearchPage(input: {text: "gateway", limit: 1}, after: $after) {
                items { kind entityId eventId } after
              }
            }""",
            {"after": cursor},
        )
        next_page = _field(continuation, "structuredSearchPage")
        assert (
            len(require_sequence(next_page.get("items"), "next search items"))
            <= 1
        )
        assert next_page.get("after") != cursor
    _field(data, "entityRelations")
    assert _field(data, "scribeModels") == {
        "defaultModel": "fixture",
        "models": [
            {
                "alias": "fixture",
                "provider": "vllm",
                "model": "e2e",
                "reasoningEfforts": ["medium"],
                "defaultReasoningEffort": None,
            }
        ],
    }
    assert any(
        require_mapping(item, "ontology").get("ontologyId") == ONTOLOGY_ID
        for item in require_sequence(
            data.get("golOntologies"), "GOL ontologies"
        )
    )
    assert any(
        require_mapping(item, "pipeline").get("pipelineId") == PIPELINE_ID
        for item in require_sequence(
            data.get("pipelineDefinitions"), "pipelineDefinitions"
        )
    )
