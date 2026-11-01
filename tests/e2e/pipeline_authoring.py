"""Live Gateway authoring proves drafts and reviews remain separate from execution."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from assertions import require_mapping, require_sequence
from clients import GatewayClient, GraphQLOperationError


async def exercise_pipeline_authoring(
    gateway: GatewayClient, token: str, outsider_token: str
) -> None:
    """Exercises the public contract through real IAM, Registry and immutable storage."""
    await exercise_plugin_ontology_authoring(gateway, token, outsider_token)
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


async def exercise_plugin_ontology_authoring(
    gateway: GatewayClient, token: str, outsider_token: str
) -> None:
    """Proves explicit tenant overlays survive the public API and Registry CAS."""
    inventory = await gateway.execute(token, "{pluginOntologies}")
    packages = require_sequence(
        require_mapping(
            json.loads(str(inventory["pluginOntologies"])), "inventory"
        ).get("packages"),
        "packages",
    )
    package = next(
        require_mapping(item, "package")
        for item in packages
        if require_mapping(item, "package").get("id") == "utils.mail"
    )
    person = {
        "version": "1",
        "resources": [
            {
                "resource_id": "tenant.person",
                "kind": "object_type",
                "display_name": "Person",
                "references": [],
                "attributes": {},
            },
            {
                "resource_id": "tenant.person.name",
                "kind": "property",
                "display_name": "Name",
                "owner_id": "tenant.person",
                "value_type": "string",
                "references": [],
                "attributes": {},
            },
        ],
    }
    draft = """mutation($id:String!,$definition:JSON!){saveOntologyDraft(ontologyId:$id,displayName:"E2E people",definition:$definition)}"""
    variables = {"id": "e2e_people", "definition": json.dumps(person)}
    with pytest.raises(GraphQLOperationError):
        await gateway.execute(outsider_token, draft, variables)
    saved = await gateway.execute(token, draft, variables)
    target_revision = saved.get("saveOntologyDraft")
    assert isinstance(target_revision, str)
    publish = """mutation($id:String!,$revision:String!){publishOntology(ontologyId:$id,displayName:"E2E ontology",revisionId:$revision,metadata:"{}") {revisionId}}"""
    await gateway.execute(
        token, publish, {"id": "e2e_people", "revision": target_revision}
    )
    preview = """query($version:String!,$target:String,$revision:String,$mappings:JSON!){pluginOntologyPreview(packageId:"utils.mail",packageVersion:$version,targetOntologyId:$target,targetRevisionId:$revision,mappings:$mappings)}"""
    selection: dict[str, object] = {
        "version": package.get("version"),
        "target": None,
        "revision": None,
        "mappings": "{}",
    }
    isolated = await gateway.execute(token, preview, selection)
    isolated_document = require_mapping(
        json.loads(str(isolated["pluginOntologyPreview"])), "isolated ontology"
    )
    assert all(
        require_mapping(item, "resource").get("resource_id") != "tenant.person"
        for item in require_sequence(
            isolated_document.get("resources"), "resources"
        )
    )
    selection.update(
        target="e2e_people",
        revision=target_revision,
        mappings=json.dumps({"mail.message.to": "tenant.person"}),
    )
    linked = await gateway.execute(token, preview, selection)
    linked_document = require_mapping(
        json.loads(str(linked["pluginOntologyPreview"])), "linked ontology"
    )
    resources = {
        require_mapping(item, "resource").get("resource_id"): require_mapping(
            item, "resource"
        )
        for item in require_sequence(
            linked_document.get("resources"), "resources"
        )
    }
    assert (
        require_mapping(
            resources["mail.message.to"].get("attributes"),
            "recipient attributes",
        ).get("target_type")
        == "tenant.person"
    )
    assert (
        require_mapping(
            resources["mail.message.sender"].get("attributes"),
            "sender attributes",
        ).get("target_type")
        == "mail.person"
    )
    assert "tenant.person.name" in resources
    write = """mutation($version:String!,$target:String,$revision:String,$mappings:JSON!,$expected:String){savePluginOntology(ontologyId:"e2e_mail",displayName:"E2E mail",packageId:"utils.mail",packageVersion:$version,targetOntologyId:$target,targetRevisionId:$revision,mappings:$mappings,expectedRevisionId:$expected)}"""
    with pytest.raises(GraphQLOperationError):
        await gateway.execute(outsider_token, write, selection)
    result = await gateway.execute(token, write, selection)
    overlay_revision = result.get("savePluginOntology")
    assert isinstance(overlay_revision, str)
    workspace_query = """{ontologyWorkspace(ontologyId:"e2e_mail")}"""
    opened = await gateway.execute(token, workspace_query)
    workspace = require_mapping(
        json.loads(str(opened["ontologyWorkspace"])), "workspace"
    )
    assert workspace.get("ontology") == linked_document
    selection["expected"] = workspace.get("revision_id")
    updated = await gateway.execute(token, write, selection)
    with pytest.raises(GraphQLOperationError):
        await gateway.execute(token, write, selection)
    await gateway.execute(
        token,
        publish,
        {"id": "e2e_mail", "revision": updated.get("savePluginOntology")},
    )
    with pytest.raises(GraphQLOperationError):
        await gateway.execute(outsider_token, workspace_query)
    assert await gateway.execute(token, "{pluginOntologies}") == inventory
    authored = await gateway.execute(
        token,
        """mutation($definition:JSON!,$command:JSON!){authorPipeline(pipelineId:"e2e_bound_mail",name:"E2E mail draft",definition:$definition,command:$command,message:"Ontology contract fixture"){headRevisionId}}""",
        {
            "definition": json.dumps(
                {
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
        """mutation($head:String!){bindPluginOntology(ontologyId:"e2e_mail",pipelineId:"e2e_bound_mail",blockId:"reply",expectedPipelineRevision:$head)}""",
        {"head": head},
    )
    assert isinstance(bound.get("bindPluginOntology"), str)
    bindings = await gateway.execute(
        token,
        """{ontologyBindings(pipelineId:"e2e_bound_mail"){blockId ontologyId includeDependencies resourceKinds}}""",
    )
    binding = require_mapping(
        require_sequence(bindings.get("ontologyBindings"), "bindings")[0],
        "binding",
    )
    assert binding.get("blockId") == "reply"
    assert binding.get("ontologyId") == "e2e_mail"
    assert binding.get("includeDependencies") is True
    assert "object_type" in json.loads(str(binding.get("resourceKinds")))
