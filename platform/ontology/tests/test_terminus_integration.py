"""Exercises native history and tenant capabilities against TerminusDB 12.0.7."""

from __future__ import annotations

import asyncio

import httpx
from galadril_ontology import (
    BaseOntologyArtifact,
    ConcurrentHeadUpdateError,
    Ontology,
    OntologyChange,
    OntologyResource,
    OntologyService,
    ResourceKind,
)
from galadril_ontology.backends.terminus import (
    TerminusClient,
    TerminusConfig,
    TerminusOntologyRepository,
)
from galadril_ontology.runtime import (
    OntologySliceRequest,
    OntologySliceSelector,
    PipelineOntologyBinding,
    PublishedOntology,
)
from testcontainers.core.container import DockerContainer


async def exercise(endpoint: str) -> None:
    async with httpx.AsyncClient(
        base_url=endpoint, auth=("admin", "root"), timeout=30
    ) as admin:
        for attempt in range(60):
            try:
                response = await admin.get("/api/info")
                response.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 59:
                    raise
                await asyncio.sleep(1)
        actions = [
            "branch",
            "instance_read_access",
            "instance_write_access",
            "schema_read_access",
            "commit_read_access",
            "commit_write_access",
            "meta_read_access",
            "meta_write_access",
        ]
        response = await admin.post(
            "/api/roles", json={"name": "test_writer", "action": actions}
        )
        response.raise_for_status()
        scopes: dict[str, dict[str, str]] = {}
        for tenant in ("tenant_a", "tenant_b", "bases"):
            response = await admin.post(
                f"/api/db/admin/{tenant}",
                json={
                    "label": tenant,
                    "schema": False,
                    "prefixes": {
                        "@base": "terminusdb:///data/",
                        "@schema": "terminusdb:///schema#",
                    },
                },
            )
            response.raise_for_status()
            response = await admin.post(
                "/api/users", json={"name": tenant, "password": "secret"}
            )
            response.raise_for_status()
            response = await admin.post(
                "/api/capabilities",
                json={
                    "operation": "grant",
                    "scope_type": "database",
                    "scope": f"admin/{tenant}",
                    "user": tenant,
                    "roles": ["test_writer"],
                },
            )
            response.raise_for_status()
            scopes[tenant] = {
                "database": tenant,
                "user": tenant,
                "password": "secret",
            }
        cfg = TerminusConfig.model_validate(
            {
                "endpoint": endpoint,
                "organization": "admin",
                "tenants": {
                    key: scopes[key] for key in ("tenant_a", "tenant_b")
                },
                "bases": scopes["bases"],
            }
        )
        client = TerminusClient(cfg)
        try:
            repository = TerminusOntologyRepository(client)
            base = BaseOntologyArtifact.from_ontology(
                Ontology(
                    version="v1",
                    resources=(
                        OntologyResource(
                            resource_id="core.person",
                            kind=ResourceKind.OBJECT_TYPE,
                            display_name="Person",
                        ),
                    ),
                )
            )
            await repository.register_base(base)
            service = OntologyService(repository)
            root = await service.initialize_tenant("tenant_a")
            assert await service.initialize_tenant("tenant_a") == root
            await service.initialize_tenant("tenant_b")
            fork = await service.create_branch("tenant_a", "experiment")
            changed = await service.commit(
                "tenant_a",
                "experiment",
                expected_head=fork.head_revision_id,
                changes=(
                    OntologyChange.set_field(
                        "core.person", ("description",), "Tenant description"
                    ),
                ),
                author="alice",
                message="Edit ontology",
            )
            assert changed.revision_id != fork.head_revision_id
            old = await service.materialize("tenant_a", root.head_revision_id)
            assert old.ontology.require("core.person").description == ""
            new = await service.materialize("tenant_a", changed.revision_id)
            assert (
                new.ontology.require("core.person").description
                == "Tenant description"
            )
            assert (await service.materialize("tenant_b")).ontology.require(
                "core.person"
            ).description == ""
            try:
                await service.commit(
                    "tenant_a",
                    "experiment",
                    expected_head=fork.head_revision_id,
                    changes=(),
                    author="alice",
                    message="Stale",
                )
            except ConcurrentHeadUpdateError:
                pass
            else:
                raise AssertionError("A stale native branch update succeeded")
            merged = await service.merge(
                "tenant_a",
                target_branch="main",
                source_branch="experiment",
                expected_target_head=root.head_revision_id,
                author="alice",
                message="Merge experiment",
            )
            assert merged.revision is not None
            assert (
                await service.materialize(
                    "tenant_a", merged.revision.revision_id
                )
            ).ontology == new.ontology
            publication = PublishedOntology(
                tenant_id="tenant_a",
                ontology_id="default",
                publication_id="a" * 32,
                materialization=await service.materialize("tenant_a"),
            )
            await repository.publish(publication)
            await repository.bind(
                PipelineOntologyBinding(
                    tenant_id="tenant_a",
                    pipeline_id="daily",
                    block_id="resolve",
                    ontology_id="default",
                    selector=OntologySliceSelector(
                        resource_ids=("core.person",)
                    ),
                )
            )
            resolved = await repository.load_runtime_slice(
                OntologySliceRequest(
                    tenant_id="tenant_a",
                    pipeline_id="daily",
                    block_id="resolve",
                )
            )
            assert (
                resolved.ontology.require("core.person").description
                == "Tenant description"
            )
            # A forged database path must fail at the server, including history.
            for route in ("document", "log"):
                denied = await admin.get(
                    f"/api/{route}/admin/tenant_b/local/branch/main",
                    auth=("tenant_a", "secret"),
                )
                assert denied.status_code == 403
            denied = await admin.get(
                f"/api/document/admin/tenant_a/local/commit/{changed.revision_id}",
                auth=("tenant_b", "secret"),
            )
            assert denied.status_code == 403
        finally:
            await client.close()


def test_terminus_native_history_and_tenant_capabilities() -> None:
    """Exercises ontology behavior against a real pinned TerminusDB server."""
    with (
        DockerContainer("terminusdb/terminusdb-server:v12.0.7")
        .with_env("TERMINUSDB_ADMIN_PASS", "root")
        .with_exposed_ports(6363) as container
    ):
        endpoint = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(6363)}"
        asyncio.run(exercise(endpoint))


def main() -> None:
    test_terminus_native_history_and_tenant_capabilities()


if __name__ == "__main__":
    main()
