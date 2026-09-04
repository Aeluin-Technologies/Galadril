"""Native branch snapshots for ontology overlays and runtime publications.

Only sparse overlays and provenance live in tenant snapshots. Immutable shared
base artifacts stay in a separate database; effective ontologies are rebuilt.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import JsonValue, TypeAdapter

from galadril_ontology.backends.terminus.client import (
    TerminusClient,
    document_named,
)
from galadril_ontology.errors import (
    BranchAlreadyExistsError,
    ConcurrentHeadUpdateError,
    OntologyNotFoundError,
)
from galadril_ontology.identity import validate_branch_name
from galadril_ontology.materialization import (
    OverlayAccumulator,
    materialize_overlay,
)
from galadril_ontology.model import (
    BaseOntologyArtifact,
    MaterializedOntology,
    MergeConflict,
    OntologyBranch,
    OntologyRevision,
    OverlaySnapshot,
    ontology_content_hash,
)
from galadril_ontology.runtime import (
    InMemoryOntologyRuntimeStore,
    OntologySlice,
    OntologySliceRequest,
    PipelineOntologyBinding,
    PublishedOntology,
)
from galadril_ontology.validation import validate_ontology

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def native_branch(name: str) -> str:
    validate_branch_name(name)
    # Hex encoding preserves distinct user branch names without path separators.
    encoded = "ontology-" + name.encode().hex()
    if len(encoded) > 128:
        raise ValueError("Ontology branch name is too long for TerminusDB")
    return encoded


class TerminusOntologyRepository:
    __slots__ = ("_client",)

    def __init__(self, client: TerminusClient) -> None:
        self._client = client

    async def register_base(self, artifact: BaseOntologyArtifact) -> None:
        head, docs = await self._client.read(None)
        identity = "base/" + artifact.version
        try:
            stored = BaseOntologyArtifact.model_validate(
                document_named(docs, identity)["artifact"]
            )
        except OntologyNotFoundError:
            await self._client.write(
                None,
                {"@id": identity, "artifact": artifact.model_dump(mode="json")},
                expected=head,
                author="platform",
                message="Register immutable base " + artifact.version,
            )
            return
        if stored.content_hash != artifact.content_hash:
            raise ValueError("Base version already has different content")

    async def get_base(self, version: str) -> BaseOntologyArtifact:
        _, docs = await self._client.read(None)
        return BaseOntologyArtifact.model_validate(
            document_named(docs, "base/" + version)["artifact"]
        )

    async def get_latest_base(self) -> BaseOntologyArtifact:
        _, docs = await self._client.read(None)
        bases = [
            BaseOntologyArtifact.model_validate(doc["artifact"])
            for doc in docs
            if "artifact" in doc
        ]
        if not bases:
            raise OntologyNotFoundError("No shared base artifact is registered")
        return max(bases, key=lambda base: base.created_at)

    async def get_branch(self, tenant_id: str, name: str) -> OntologyBranch:
        head, docs = await self._client.read(tenant_id, ref=native_branch(name))
        document_named(docs, "ontology/state")
        return OntologyBranch(
            tenant_id=tenant_id, name=name, head_revision_id=head
        )

    async def initialize_tenant(
        self,
        revision: OntologyRevision,
        branch: OntologyBranch,
        materialization: MaterializedOntology,
    ) -> OntologyBranch:
        reference = native_branch(branch.name)
        try:
            await self._client.request(
                revision.tenant_id,
                "POST",
                "branch",
                ref=reference,
                body={"origin": self._client.path(revision.tenant_id)},
            )
        except BranchAlreadyExistsError:
            pass
        head, docs = await self._client.read(revision.tenant_id, ref=reference)
        if any("revision" in doc for doc in docs):
            return OntologyBranch(
                tenant_id=revision.tenant_id,
                name=branch.name,
                head_revision_id=head,
            )
        committed = await self.commit_revision(
            branch.name, head, revision, materialization
        )
        return branch.model_copy(
            update={"head_revision_id": committed.revision_id}
        )

    async def create_branch(self, branch: OntologyBranch) -> None:
        await self.get_revision(branch.tenant_id, branch.head_revision_id)
        try:
            await self._client.request(
                branch.tenant_id,
                "POST",
                "branch",
                ref=native_branch(branch.name),
                body={
                    "origin": self._client.path(
                        branch.tenant_id, branch.head_revision_id, commit=True
                    )
                },
            )
        except ConcurrentHeadUpdateError as error:
            raise BranchAlreadyExistsError(
                "Ontology branch already exists"
            ) from error

    async def get_revision(
        self, tenant_id: str, revision_id: str
    ) -> OntologyRevision:
        _, docs = await self._client.read(
            tenant_id, ref=revision_id, commit=True
        )
        data = _JSON_OBJECT.validate_python(
            document_named(docs, "ontology/state")["revision"]
        )
        if data.get("tenant_id") != tenant_id:
            raise OntologyNotFoundError(
                "Tenant ontology revision is unavailable"
            )
        return OntologyRevision.model_validate(
            {**data, "revision_id": revision_id}
        )

    async def commit_revision(
        self,
        branch_name: str,
        expected_head: str,
        revision: OntologyRevision,
        materialization: MaterializedOntology,
    ) -> OntologyRevision:
        if (
            materialization.tenant_id != revision.tenant_id
            or materialization.revision_id != revision.revision_id
        ):
            raise ValueError("Revision and materialization scope differ")
        validate_ontology(materialization.ontology)
        document: dict[str, JsonValue] = {
            "@id": "ontology/state",
            "revision": revision.model_dump(
                mode="json", exclude={"revision_id"}
            ),
            "materialization": materialization.model_dump(
                mode="json", exclude={"revision_id", "ontology"}
            ),
        }
        head = await self._client.write(
            revision.tenant_id,
            document,
            expected=expected_head,
            ref=native_branch(branch_name),
            author=revision.author,
            message=revision.message,
        )
        return revision.model_copy(update={"revision_id": head})

    async def get_materialization(
        self, tenant_id: str, revision_id: str
    ) -> MaterializedOntology:
        _, docs = await self._client.read(
            tenant_id, ref=revision_id, commit=True
        )
        data = _JSON_OBJECT.validate_python(
            document_named(docs, "ontology/state")["materialization"]
        )
        if data.get("tenant_id") != tenant_id:
            raise OntologyNotFoundError(
                "Tenant ontology materialization is unavailable"
            )
        artifact = await self.get_base(str(data["base_version"]))
        if artifact.content_hash != data["base_hash"]:
            raise ValueError(
                "Shared base hash does not match the pinned revision"
            )
        overlay = OverlaySnapshot.model_validate(data["overlay"])
        effective = materialize_overlay(
            artifact.ontology,
            OverlayAccumulator.from_snapshot(overlay),
            effective_version=artifact.version,
        )
        validate_ontology(effective)
        if ontology_content_hash(effective) != data["effective_hash"]:
            raise ValueError(
                "Effective ontology does not match the committed hash"
            )
        return MaterializedOntology.model_validate(
            {**data, "revision_id": revision_id, "ontology": effective}
        )

    async def put_materialization(
        self, materialization: MaterializedOntology
    ) -> None:
        # Immutable native snapshots already contain the sparse state; verify it
        # instead of introducing a second mutable materialization authority.
        stored = await self.get_materialization(
            materialization.tenant_id, materialization.revision_id
        )
        if stored != materialization:
            raise ValueError("Cannot modify an immutable ontology snapshot")

    async def find_merge_base(
        self, tenant_id: str, left_revision: str, right_revision: str
    ) -> str:
        async def ancestors(revision: str) -> dict[str, int]:
            _, log = await self._client.request(
                tenant_id,
                "GET",
                "log",
                ref=revision,
                commit=True,
                params={"count": "-1"},
            )
            commits = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
                log
            )
            return {
                str(item["identifier"]): index
                for index, item in enumerate(commits)
            }

        left = await ancestors(left_revision)
        right = await ancestors(right_revision)
        common = left.keys() & right.keys()
        if not common:
            raise OntologyNotFoundError(
                "Ontology branches have no common ancestor"
            )
        # TerminusDB commits semantic merges as squashes. Remember accepted
        # source commits through immutable provenance, so subsequent merges
        # compare only source changes made since that accepted snapshot.
        accepted_sources: set[str] = set()
        for identifier in left:
            if identifier in common:
                break
            revision = await self.get_revision(tenant_id, identifier)
            if len(revision.parents) == 2 and revision.parents[1] in right:
                accepted_sources.add(revision.parents[1])
        if accepted_sources:
            return min(accepted_sources | common, key=lambda item: right[item])
        return min(
            common,
            key=lambda item: (
                max(left[item], right[item]),
                left[item] + right[item],
                item,
            ),
        )

    async def save_conflicts(
        self,
        tenant_id: str,
        merge_id: str,
        conflicts: tuple[MergeConflict, ...],
    ) -> None:
        head, _ = await self._client.read(tenant_id)
        await self._client.write(
            tenant_id,
            {
                "@id": "conflict/" + merge_id,
                "conflicts": [
                    item.model_dump(mode="json") for item in conflicts
                ],
            },
            expected=head,
            author="platform",
            message="Record semantic merge conflicts",
        )

    async def list_conflicts(
        self, tenant_id: str, merge_id: str
    ) -> tuple[MergeConflict, ...]:
        _, docs = await self._client.read(tenant_id)
        try:
            data = document_named(docs, "conflict/" + merge_id)["conflicts"]
        except OntologyNotFoundError:
            return ()
        return TypeAdapter(tuple[MergeConflict, ...]).validate_python(data)

    async def publish(self, publication: PublishedOntology) -> None:
        tenant = publication.tenant_id
        materialization = await self.get_materialization(
            tenant, publication.materialization.revision_id
        )
        if materialization != publication.materialization:
            raise ValueError(
                "Publication must reference the validated native snapshot"
            )
        revision = await self.get_revision(tenant, materialization.revision_id)
        head, _ = await self._client.read(tenant)
        data: dict[str, JsonValue] = {
            "@id": "ontology/" + publication.ontology_id,
            "ontology_id": publication.ontology_id,
            "display_name": publication.ontology_id,
            "publication": {
                "publication_id": publication.publication_id,
                "revision_id": revision.revision_id,
                "lifecycle": "production",
                "metadata": publication.metadata,
                "base_version": revision.base_version,
                "base_hash": revision.base_hash,
                "effective_hash": materialization.effective_hash,
                "author": revision.author,
                "message": revision.message,
                "published_at_ms": int(
                    publication.published_at.timestamp() * 1000
                ),
                "retired_at_ms": None,
            },
        }
        await self._client.write(
            tenant,
            data,
            expected=head,
            author=revision.author,
            message="Publish ontology",
        )

    async def bind(self, binding: PipelineOntologyBinding) -> None:
        head, docs = await self._client.read(binding.tenant_id)
        entry = document_named(docs, "ontology/" + binding.ontology_id)
        if (
            _JSON_OBJECT.validate_python(entry["publication"]).get("lifecycle")
            != "production"
        ):
            raise OntologyNotFoundError("Tenant ontology is not published")
        await self._client.write(
            binding.tenant_id,
            {
                "@id": f"binding/{binding.pipeline_id}/{binding.block_id}",
                "binding": binding.model_dump(mode="json"),
                "updated_at_ms": int(datetime.now(UTC).timestamp() * 1000),
            },
            expected=head,
            author="platform",
            message="Bind pipeline ontology",
        )

    async def load_runtime_slice(
        self, request: OntologySliceRequest
    ) -> OntologySlice:
        _, docs = await self._client.read(request.tenant_id)
        binding = PipelineOntologyBinding.model_validate(
            document_named(
                docs, f"binding/{request.pipeline_id}/{request.block_id}"
            )["binding"]
        )
        if binding.tenant_id != request.tenant_id:
            raise OntologyNotFoundError("Tenant binding is unavailable")
        data = _JSON_OBJECT.validate_python(
            document_named(docs, "ontology/" + binding.ontology_id)[
                "publication"
            ]
        )
        if data["lifecycle"] != "production":
            raise OntologyNotFoundError("Tenant ontology is not published")
        materialization = await self.get_materialization(
            request.tenant_id, str(data["revision_id"])
        )
        publication = PublishedOntology(
            tenant_id=request.tenant_id,
            ontology_id=binding.ontology_id,
            publication_id=str(data["publication_id"]),
            materialization=materialization,
            metadata=_JSON_OBJECT.validate_python(data["metadata"]),
            published_at=datetime.fromtimestamp(
                float(str(data["published_at_ms"])) / 1000, UTC
            ),
        )
        store = InMemoryOntologyRuntimeStore()
        await store.publish(publication)
        await store.bind(binding)
        return await store.load_runtime_slice(request)
