"""Live Gateway authoring proves drafts and reviews remain separate from execution."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from assertions import require_mapping, require_sequence
from clients import GatewayClient, GraphQLOperationError
from environment import runfile
from gol_authoring import gol_access, publish_gol_source


async def exercise_pipeline_authoring(
    gateway: GatewayClient, token: str, outsider_token: str
) -> None:
    """Exercises the public contract through real IAM, Registry and immutable storage."""
    await exercise_gol_plugin_authoring(gateway, token, outsider_token)
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


async def exercise_gol_plugin_authoring(
    gateway: GatewayClient, token: str, outsider_token: str
) -> None:
    """Publishes the real Utils GOL package and a typed dependent ontology."""
    plugin_source = runfile(
        "plugins/src/galadril_plugins/utils/mail.gol"
    ).read_text()
    package = await publish_gol_source(
        gateway, token, "communication.mail", plugin_source
    )
    source = """ontology e2e.mail version "1.0.0" {
    import communication.mail version "1.0.0";
    entity LinkedMessage { field message: Ref<communication.mail.Message>; }
}"""
    source = source.removesuffix("}") + gol_access("LinkedMessage") + "\n}"
    saved = await publish_gol_source(gateway, token, "e2e.mail", source)
    query = 'query{golOntologyWorkspace(ontologyId:"e2e.mail"){summary{sourceRevision activeGeneration} files{path text}}}'
    with pytest.raises(GraphQLOperationError):
        await gateway.execute(outsider_token, query)
    opened = require_mapping(
        (await gateway.execute(token, query)).get("golOntologyWorkspace"),
        "GOL workspace",
    )
    assert (
        require_mapping(opened.get("summary"), "GOL summary").get(
            "activeGeneration"
        )
        == saved.generation
    )
    assert (
        require_mapping(
            require_sequence(opened.get("files"), "source files")[0],
            "source file",
        ).get("text")
        == source
    )
    replacement = await publish_gol_source(
        gateway,
        token,
        "e2e.mail",
        source.replace(
            "field message:", "field title: Option<String>; field message:"
        ),
        expected_revision=saved.source_revision,
        expected_generation=saved.generation,
    )
    assert replacement.generation != saved.generation
    with pytest.raises(GraphQLOperationError):
        await publish_gol_source(
            gateway,
            token,
            "e2e.mail",
            source,
            expected_revision=saved.source_revision,
            expected_generation=saved.generation,
        )
    dependency = await gateway.execute(
        token,
        'mutation($revision:String!){validateGolOntology(ontologyId:"e2e.mail",sourceRevision:$revision){status dependencies{ontologyId generation}}}',
        {"revision": replacement.source_revision},
    )
    checked = require_mapping(
        dependency.get("validateGolOntology"), "GOL dependencies"
    )
    assert checked.get("status") == "valid"
    assert any(
        require_mapping(item, "dependency").get("ontologyId")
        == "communication.mail"
        and require_mapping(item, "dependency").get("generation")
        == package.generation
        for item in require_sequence(
            checked.get("dependencies"), "dependencies"
        )
    )
    authored = await gateway.execute(
        token,
        'mutation($definition:JSON!,$command:JSON!){authorPipeline(pipelineId:"e2e_bound_mail",name:"E2E mail draft",definition:$definition,command:$command,message:"GOL contract fixture"){headRevisionId}}',
        {
            "definition": json.dumps(
                {
                    "name": "E2E mail draft",
                    "sources": [{"id": "input"}],
                    "pipeline": [
                        {
                            "step": "reply",
                            "type": "sink",
                            "input_from": ["input"],
                        }
                    ],
                }
            ),
            "command": json.dumps(
                {"operation": "save_draft", "branch": "main"}
            ),
        },
    )
    head = require_mapping(authored.get("authorPipeline"), "mail draft").get(
        "headRevisionId"
    )
    bound = await gateway.execute(
        token,
        'mutation($head:String!){bindGolOntology(ontologyId:"e2e.mail",pipelineId:"e2e_bound_mail",blockId:"reply",expectedPipelineRevision:$head)}',
        {"head": head},
    )
    assert isinstance(bound.get("bindGolOntology"), str)
    data = await gateway.execute(
        token,
        '{ontologyBindings(pipelineId:"e2e_bound_mail"){blockId ontologyId generation sourceRevision symbols}}',
    )
    binding = require_mapping(
        require_sequence(data.get("ontologyBindings"), "bindings")[0], "binding"
    )
    assert binding.get("blockId") == "reply"
    assert binding.get("ontologyId") == "e2e.mail"
    assert binding.get("generation") == replacement.generation
    assert binding.get("sourceRevision") == replacement.source_revision
    assert "e2e.mail::LinkedMessage" in require_sequence(
        binding.get("symbols"), "bound symbols"
    )
