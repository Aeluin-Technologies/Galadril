"""Live Gateway authoring proves drafts and reviews remain separate from execution."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from assertions import require_mapping
from clients import GatewayClient, GraphQLOperationError


async def exercise_pipeline_authoring(
    gateway: GatewayClient, token: str, outsider_token: str
) -> None:
    """Exercises the public contract through real IAM, Registry and immutable storage."""
    identifier = "e2e_authoring"
    incomplete = {"name": "E2E authoring", "sources": [], "pipeline": []}
    query = """
      mutation Author($id:String!,$name:String!,$definition:JSON!,$command:JSON!,$head:String) {
        authorPipeline(pipelineId:$id,name:$name,definition:$definition,command:$command,
          expectedHeadRevisionId:$head,message:"E2E authoring transition") {
          pipelineId headRevisionId publishedRevisionId lifecycle authoring definition
        }
      }
    """

    async def author(
        command: Mapping[str, object],
        definition: Mapping[str, object],
        head: str | None,
        principal: str = token,
    ) -> Mapping[str, object]:
        response = await gateway.execute(
            principal,
            query,
            {
                "id": identifier,
                "name": "E2E authoring",
                "definition": json.dumps(definition),
                "command": json.dumps(command),
                "head": head,
            },
        )
        return require_mapping(
            response.get("authorPipeline"), "authored pipeline"
        )

    value = await author(
        {"operation": "save_draft", "branch": "main"}, incomplete, None
    )
    head = value.get("headRevisionId")
    assert isinstance(head, str) and value.get("lifecycle") == "draft"
    assert value.get("publishedRevisionId") is None
    with pytest.raises(GraphQLOperationError):
        await author({"operation": "approve"}, incomplete, head)
    with pytest.raises(GraphQLOperationError):
        await author(
            {"operation": "save_draft", "branch": "main"},
            incomplete,
            head,
            outsider_token,
        )
    complete = {
        "name": "E2E authoring",
        "sources": [
            {
                "id": "input",
                "topic": "raw",
                "match_pattern": "pipeline-authoring-no-ingest$",
                "schema_path": "schemas/avro/text.avsc",
            }
        ],
        "pipeline": [
            {
                "step": "union",
                "type": "forward",
                "input_from": ["input"],
                "params": {"merge": "union"},
            }
        ],
        "editor": {
            "positions": {
                "input": {"x": 40, "y": 80},
                "union": {"x": 360, "y": 80},
            }
        },
    }
    for command in [
        {"operation": "save_draft", "branch": "main"},
        {
            "operation": "create_branch",
            "branch": "review",
            "from_branch": "main",
        },
        {"operation": "propose_merge", "source_branch": "review"},
        {"operation": "merge", "proposal_id": "review", "resolutions": {}},
        {"operation": "approve"},
        {"operation": "publish"},
    ]:
        value = await author(command, complete, head)
        head = value.get("headRevisionId")
        assert isinstance(head, str)
        assert value.get("publishedRevisionId") is None
    assert value.get("lifecycle") == "published"
    value = await author({"operation": "deploy"}, complete, head)
    head = value.get("headRevisionId")
    assert isinstance(head, str) and value.get("lifecycle") == "deployed"
    assert isinstance(value.get("publishedRevisionId"), str)
    retired = await gateway.execute(
        token,
        "mutation($id:String!,$head:String!){deletePipeline(pipelineId:$id,expectedHeadRevisionId:$head)}",
        {"id": identifier, "head": head},
    )
    assert retired.get("deletePipeline") is True
