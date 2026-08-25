"""Exhaustive in-memory repository consistency-boundary tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from galadril_ontology import (
    BaseOntologyArtifact,
    InMemoryOntologyRepository,
    MaterializedOntology,
    Ontology,
    OntologyBranch,
    OntologyResource,
    OntologyRevision,
    OverlaySnapshot,
    ResourceKind,
)
from galadril_ontology.errors import (
    BranchAlreadyExistsError,
    ConcurrentHeadUpdateError,
    OntologyNotFoundError,
)
from galadril_ontology.model import ontology_content_hash


def _artifact(description: str = "base") -> BaseOntologyArtifact:
    return BaseOntologyArtifact.from_ontology(
        Ontology(
            version="v1",
            resources=(
                OntologyResource(
                    resource_id="core.customer",
                    kind=ResourceKind.OBJECT_TYPE,
                    display_name="Customer",
                    description=description,
                ),
            ),
        )
    )


def _revision(
    revision_id: str,
    artifact: BaseOntologyArtifact,
    *,
    parents: tuple[str, ...] = (),
) -> OntologyRevision:
    return OntologyRevision(
        tenant_id="tenant-a",
        revision_id=revision_id,
        base_version=artifact.version,
        base_hash=artifact.content_hash,
        parents=parents,
        author="test",
        message="test",
    )


def _materialization(
    revision: OntologyRevision, artifact: BaseOntologyArtifact
) -> MaterializedOntology:
    return MaterializedOntology(
        tenant_id=revision.tenant_id,
        revision_id=revision.revision_id,
        base_version=artifact.version,
        base_hash=artifact.content_hash,
        effective_hash=ontology_content_hash(artifact.ontology),
        overlay=OverlaySnapshot(),
        ontology=artifact.ontology,
    )


@pytest.mark.asyncio
async def test_repository_base_and_initialization_error_paths() -> None:
    repository = InMemoryOntologyRepository()
    with pytest.raises(OntologyNotFoundError, match="no base"):
        await repository.get_latest_base()
    with pytest.raises(OntologyNotFoundError, match="artifact"):
        await repository.get_base("missing")
    artifact = _artifact()
    await repository.register_base(artifact)
    with pytest.raises(ValueError, match="different content"):
        await repository.register_base(_artifact("different"))

    invalid = _revision("1" * 32, artifact, parents=("0" * 32,))
    branch = OntologyBranch(
        tenant_id="tenant-a", name="main", head_revision_id=invalid.revision_id
    )
    with pytest.raises(OntologyNotFoundError, match="parent"):
        await repository.initialize_tenant(
            invalid, branch, _materialization(invalid, artifact)
        )

    root = _revision("2" * 32, artifact)
    root_branch = branch.model_copy(
        update={"head_revision_id": root.revision_id}
    )
    materialization = _materialization(root, artifact)
    assert (
        await repository.initialize_tenant(root, root_branch, materialization)
        == root_branch
    )
    assert (
        await repository.initialize_tenant(root, root_branch, materialization)
        == root_branch
    )
    with pytest.raises(OntologyNotFoundError):
        await repository.get_revision("tenant-a", "f" * 32)
    with pytest.raises(OntologyNotFoundError):
        await repository.get_branch("tenant-a", "missing")


@pytest.mark.asyncio
async def test_repository_branch_commit_and_graph_error_paths() -> None:
    repository = InMemoryOntologyRepository()
    artifact = _artifact()
    await repository.register_base(artifact)
    root = _revision("1" * 32, artifact)
    main = OntologyBranch(
        tenant_id="tenant-a", name="main", head_revision_id=root.revision_id
    )
    await repository.initialize_tenant(
        root, main, _materialization(root, artifact)
    )

    with pytest.raises(OntologyNotFoundError, match="revision"):
        await repository.create_branch(
            OntologyBranch(
                tenant_id="tenant-a",
                name="missing",
                head_revision_id="f" * 32,
            )
        )
    await repository.create_branch(
        main.model_copy(update={"name": "experiment"})
    )
    with pytest.raises(BranchAlreadyExistsError):
        await repository.create_branch(
            main.model_copy(update={"name": "experiment"})
        )

    child = _revision("2" * 32, artifact, parents=(root.revision_id,))
    with pytest.raises(OntologyNotFoundError, match="branch"):
        await repository.commit_revision(
            "missing",
            root.revision_id,
            child,
            _materialization(child, artifact),
        )
    with pytest.raises(ConcurrentHeadUpdateError):
        await repository.commit_revision(
            "main", "f" * 32, child, _materialization(child, artifact)
        )
    await repository.commit_revision(
        "main", root.revision_id, child, _materialization(child, artifact)
    )
    duplicate = _revision(
        root.revision_id, artifact, parents=(child.revision_id,)
    )
    with pytest.raises(ValueError, match="already exists"):
        await repository.commit_revision(
            "main",
            child.revision_id,
            duplicate,
            _materialization(duplicate, artifact),
        )

    missing_parent = _revision("3" * 32, artifact, parents=("f" * 32,))
    with pytest.raises(OntologyNotFoundError, match="parent"):
        await repository.commit_revision(
            "main",
            child.revision_id,
            missing_parent,
            _materialization(missing_parent, artifact),
        )

    await repository.put_materialization(_materialization(child, artifact))
    disjoint_id = uuid4().hex
    repository._revisions[("tenant-a", disjoint_id)] = _revision(
        disjoint_id, artifact
    )
    with pytest.raises(OntologyNotFoundError, match="common ancestor"):
        await repository.find_merge_base(
            "tenant-a", child.revision_id, disjoint_id
        )
    with pytest.raises(OntologyNotFoundError, match="revision"):
        await repository._ancestor_distances("tenant-a", "e" * 32)
    merge_id = "4" * 32
    repository._revisions[("tenant-a", merge_id)] = _revision(
        merge_id,
        artifact,
        parents=(child.revision_id, root.revision_id),
    )
    distances = await repository._ancestor_distances("tenant-a", merge_id)
    assert distances[root.revision_id] == 1
