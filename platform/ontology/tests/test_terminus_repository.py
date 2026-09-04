"""Native ontology persistence keeps sparse overlays and commit identities."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_ontology import (
    BaseOntologyArtifact,
    BranchAlreadyExistsError,
    ConcurrentHeadUpdateError,
    MaterializedOntology,
    Ontology,
    OntologyBranch,
    OntologyNotFoundError,
    OntologyResource,
    OntologyRevision,
    OntologyService,
    OverlaySnapshot,
    PipelineOntologyBinding,
    PublishedOntology,
    ResourceKind,
)
from galadril_ontology.backends.terminus import (
    TerminusOntologyRepository,
    native_branch,
)
from galadril_ontology.model import ontology_content_hash
from galadril_ontology.runtime import (
    OntologySliceRequest,
    OntologySliceSelector,
)
from pydantic import JsonValue


def _artifacts() -> tuple[BaseOntologyArtifact, BaseOntologyArtifact]:
    first = BaseOntologyArtifact.from_ontology(Ontology(version="v1"))
    second = BaseOntologyArtifact.from_ontology(
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
    return first, second


def _snapshot() -> tuple[
    BaseOntologyArtifact, OntologyRevision, MaterializedOntology
]:
    base, _ = _artifacts()
    revision = OntologyRevision(
        tenant_id="tenant_a",
        revision_id="r" * 32,
        base_version=base.version,
        base_hash=base.content_hash,
        author="alice",
        message="Initialize",
    )
    materialization = MaterializedOntology(
        tenant_id="tenant_a",
        revision_id=revision.revision_id,
        base_version=base.version,
        base_hash=base.content_hash,
        effective_hash=ontology_content_hash(base.ontology),
        overlay=OverlaySnapshot(),
        ontology=base.ontology,
    )
    return base, revision, materialization


def _state_document(
    revision: OntologyRevision, materialization: MaterializedOntology
) -> dict[str, JsonValue]:
    return {
        "@id": "ontology/state",
        "revision": revision.model_dump(mode="json", exclude={"revision_id"}),
        "materialization": materialization.model_dump(
            mode="json", exclude={"revision_id", "ontology"}
        ),
    }


def test_native_commit_replaces_provisional_revision_and_stores_sparse_state() -> (
    None
):
    async def scenario() -> None:
        client = AsyncMock()
        client.path = MagicMock(
            return_value="admin/tenant_a/local/commit/" + "n" * 32
        )
        base = BaseOntologyArtifact.from_ontology(Ontology(version="v1"))
        repository = TerminusOntologyRepository(client)
        client.read.return_value = ("s" * 32, [])
        client.write.return_value = "n" * 32
        await repository.register_base(base)
        client.read.side_effect = [
            ("s" * 32, []),
            (
                "basecommit",
                [{"@id": "base/v1", "artifact": base.model_dump(mode="json")}],
            ),
            ("s" * 32, []),
        ]
        service = OntologyService(repository)
        branch = await service.initialize_tenant("tenant_a")
        assert branch.head_revision_id == "n" * 32
        call = client.write.call_args
        assert call.kwargs["expected"] == "s" * 32
        assert call.kwargs["ref"] == "ontology-6d61696e"
        document = call.args[1]
        assert "ontology" not in document["materialization"]
        assert "revision_id" not in document["revision"]
        assert "revision_id" not in document["materialization"]
        client.read.side_effect = None
        client.read.return_value = (
            "n" * 32,
            [{"@id": "ontology/state", "revision": document["revision"]}],
        )
        await repository.create_branch(
            OntologyBranch(
                tenant_id="tenant_a",
                name="experiment",
                head_revision_id="n" * 32,
            )
        )
        assert client.request.call_args.kwargs["body"]["origin"].endswith(
            "/local/commit/" + "n" * 32
        )

    asyncio.run(scenario())


def test_repeated_squash_merge_uses_previously_merged_source_commit() -> None:
    async def scenario() -> None:
        client = AsyncMock()
        root, prior_source, current_source, target = (
            letter * 32 for letter in "abcd"
        )
        client.request.side_effect = [
            (None, [{"identifier": target}, {"identifier": root}]),
            (
                None,
                [
                    {"identifier": current_source},
                    {"identifier": prior_source},
                    {"identifier": root},
                ],
            ),
        ]
        client.read.return_value = (
            target,
            [
                {
                    "@id": "ontology/state",
                    "revision": {
                        "tenant_id": "tenant_a",
                        "base_version": "v1",
                        "base_hash": "a" * 64,
                        "parents": [root, prior_source],
                        "author": "alice",
                        "message": "Merge",
                    },
                }
            ],
        )
        repository = TerminusOntologyRepository(client)
        assert (
            await repository.find_merge_base("tenant_a", target, current_source)
            == prior_source
        )

    asyncio.run(scenario())


def test_native_repository_rejects_invalid_or_conflicting_history() -> None:
    async def scenario() -> None:
        base, conflicting = _artifacts()
        client = AsyncMock()
        client.path = MagicMock(
            return_value="admin/tenant_a/local/commit/" + "r" * 32
        )
        repository = TerminusOntologyRepository(client)

        with pytest.raises(ValueError, match="too long"):
            native_branch("x" * 64)

        client.read.return_value = (
            "h" * 32,
            [
                {
                    "@id": "base/v1",
                    "artifact": conflicting.model_dump(mode="json"),
                }
            ],
        )
        with pytest.raises(ValueError, match="different content"):
            await repository.register_base(base)

        client.read.return_value = ("h" * 32, [])
        with pytest.raises(OntologyNotFoundError, match="No shared base"):
            await repository.get_latest_base()

        _, revision, materialization = _snapshot()
        state = _state_document(revision, materialization)
        client.request.side_effect = BranchAlreadyExistsError("exists")
        client.read.return_value = (revision.revision_id, [state])
        branch = await repository.initialize_tenant(
            revision,
            OntologyBranch(
                tenant_id="tenant_a",
                name="main",
                head_revision_id=revision.revision_id,
            ),
            materialization,
        )
        assert branch.head_revision_id == revision.revision_id

        client.request.side_effect = ConcurrentHeadUpdateError("changed")
        with pytest.raises(BranchAlreadyExistsError):
            await repository.create_branch(
                OntologyBranch(
                    tenant_id="tenant_a",
                    name="experiment",
                    head_revision_id=revision.revision_id,
                )
            )

        forged = revision.model_copy(update={"tenant_id": "tenant_b"})
        client.read.return_value = (
            revision.revision_id,
            [_state_document(forged, materialization)],
        )
        with pytest.raises(OntologyNotFoundError, match="revision"):
            await repository.get_revision("tenant_a", revision.revision_id)

        with pytest.raises(ValueError, match="scope differ"):
            await repository.commit_revision(
                "main",
                revision.revision_id,
                revision,
                materialization.model_copy(update={"tenant_id": "tenant_b"}),
            )

        client.request.side_effect = [
            (None, [{"identifier": "left"}]),
            (None, [{"identifier": "right"}]),
        ]
        with pytest.raises(OntologyNotFoundError, match="common ancestor"):
            await repository.find_merge_base("tenant_a", "left", "right")

    asyncio.run(scenario())


def test_native_materialization_integrity_checks_are_fail_closed() -> None:
    async def scenario() -> None:
        base, revision, materialization = _snapshot()
        client = AsyncMock()
        repository = TerminusOntologyRepository(client)
        state = _state_document(revision, materialization)

        forged = materialization.model_copy(update={"tenant_id": "tenant_b"})
        client.read.return_value = (
            revision.revision_id,
            [_state_document(revision, forged)],
        )
        with pytest.raises(OntologyNotFoundError, match="materialization"):
            await repository.get_materialization(
                "tenant_a", revision.revision_id
            )

        _, wrong_base = _artifacts()
        client.read.side_effect = [
            (revision.revision_id, [state]),
            (
                "b" * 32,
                [
                    {
                        "@id": "base/v1",
                        "artifact": wrong_base.model_dump(mode="json"),
                    }
                ],
            ),
        ]
        with pytest.raises(ValueError, match="Shared base hash"):
            await repository.get_materialization(
                "tenant_a", revision.revision_id
            )

        invalid_hash = materialization.model_copy(
            update={"effective_hash": "f" * 64}
        )
        client.read.side_effect = [
            (
                revision.revision_id,
                [_state_document(revision, invalid_hash)],
            ),
            (
                "b" * 32,
                [{"@id": "base/v1", "artifact": base.model_dump(mode="json")}],
            ),
        ]
        with pytest.raises(ValueError, match="committed hash"):
            await repository.get_materialization(
                "tenant_a", revision.revision_id
            )

        with patch.object(
            TerminusOntologyRepository,
            "get_materialization",
            AsyncMock(
                return_value=materialization.model_copy(
                    update={"effective_hash": "e" * 64}
                )
            ),
        ):
            with pytest.raises(ValueError, match="immutable"):
                await repository.put_materialization(materialization)

    asyncio.run(scenario())


def test_native_catalog_and_conflict_boundaries_reject_forged_state() -> None:
    async def scenario() -> None:
        _, revision, materialization = _snapshot()
        client = AsyncMock()
        client.read.return_value = (revision.revision_id, [])
        repository = TerminusOntologyRepository(client)

        await repository.save_conflicts("tenant_a", "merge", ())
        assert client.write.await_count == 1
        assert await repository.list_conflicts("tenant_a", "missing") == ()

        client.read.return_value = (
            revision.revision_id,
            [{"@id": "conflict/merge", "conflicts": []}],
        )
        assert await repository.list_conflicts("tenant_a", "merge") == ()

        publication = PublishedOntology(
            tenant_id="tenant_a",
            ontology_id="default",
            publication_id="a" * 32,
            materialization=materialization,
        )
        with patch.object(
            TerminusOntologyRepository,
            "get_materialization",
            AsyncMock(
                return_value=materialization.model_copy(
                    update={"effective_hash": "e" * 64}
                )
            ),
        ):
            with pytest.raises(ValueError, match="validated native"):
                await repository.publish(publication)

        binding = PipelineOntologyBinding(
            tenant_id="tenant_a",
            pipeline_id="daily",
            block_id="resolve",
            ontology_id="default",
            selector=OntologySliceSelector(resource_ids=("core.person",)),
        )
        client.read.return_value = (
            revision.revision_id,
            [
                {
                    "@id": "ontology/default",
                    "publication": {"lifecycle": "retired"},
                }
            ],
        )
        with pytest.raises(OntologyNotFoundError, match="not published"):
            await repository.bind(binding)

        forged_binding = binding.model_copy(update={"tenant_id": "tenant_b"})
        client.read.return_value = (
            revision.revision_id,
            [
                {
                    "@id": "binding/daily/resolve",
                    "binding": forged_binding.model_dump(mode="json"),
                }
            ],
        )
        request = OntologySliceRequest(
            tenant_id="tenant_a", pipeline_id="daily", block_id="resolve"
        )
        with pytest.raises(OntologyNotFoundError, match="binding"):
            await repository.load_runtime_slice(request)

        client.read.return_value = (
            revision.revision_id,
            [
                {
                    "@id": "binding/daily/resolve",
                    "binding": binding.model_dump(mode="json"),
                },
                {
                    "@id": "ontology/default",
                    "publication": {"lifecycle": "retired"},
                },
            ],
        )
        with pytest.raises(OntologyNotFoundError, match="not published"):
            await repository.load_runtime_slice(request)

    asyncio.run(scenario())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
