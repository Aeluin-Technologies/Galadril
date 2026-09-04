"""Tenant ontology application service and revision graph operations."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import structlog

from galadril_ontology.errors import (
    BaseVersionMismatchError,
    ConcurrentHeadUpdateError,
    OntologyError,
    OntologyNotFoundError,
    OntologyValidationError,
)
from galadril_ontology.identity import normalize_tenant_id
from galadril_ontology.materialization import (
    OverlayAccumulator,
    apply_changes,
    changes_between_overlays,
    materialize_overlay,
    overlay_from_effective,
)
from galadril_ontology.merge import semantic_diff, three_way_merge
from galadril_ontology.model import (
    BaseOntologyArtifact,
    ConflictKind,
    ConflictValue,
    MaterializedOntology,
    MergeConflict,
    MergeResult,
    Ontology,
    OntologyBranch,
    OntologyChange,
    OntologyRevision,
    ontology_content_hash,
)
from galadril_ontology.repository import OntologyRepository
from galadril_ontology.validation import validate_ontology

logger = structlog.get_logger(__name__)

OntologyValidator = Callable[[Ontology], None]


class BaseSynchronizationConflictError(OntologyError):
    """Carries structured conflicts against a newer platform base artifact."""

    def __init__(self, conflicts: tuple[MergeConflict, ...]) -> None:
        self.conflicts = conflicts
        super().__init__("base ontology synchronization has conflicts")


class OntologyService:
    """Coordinates validation, history, merge, and transactional branch refs."""

    __slots__ = ("_repository", "_validators")

    def __init__(
        self,
        repository: OntologyRepository,
        *,
        validators: tuple[OntologyValidator, ...] = (),
    ) -> None:
        self._repository = repository
        self._validators = validators

    def _validate(self, ontology: Ontology) -> None:
        validate_ontology(ontology)
        for validator in self._validators:
            validator(ontology)

    def _materialization(
        self,
        revision: OntologyRevision,
        artifact: BaseOntologyArtifact,
        overlay: OverlayAccumulator,
    ) -> MaterializedOntology:
        if revision.base_hash != artifact.content_hash:
            raise BaseVersionMismatchError(
                "revision base hash does not match its immutable artifact"
            )
        effective = materialize_overlay(
            artifact.ontology,
            overlay,
            effective_version=artifact.version,
        )
        self._validate(effective)
        return MaterializedOntology(
            tenant_id=revision.tenant_id,
            revision_id=revision.revision_id,
            base_version=artifact.version,
            base_hash=artifact.content_hash,
            effective_hash=ontology_content_hash(effective),
            overlay=overlay.snapshot(),
            ontology=effective,
        )

    async def initialize_tenant(
        self,
        tenant_id: str,
        *,
        author: str = "platform",
        message: str = "Initialize tenant ontology",
    ) -> OntologyBranch:
        """Creates one empty-overlay root without copying the base per tenant."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        try:
            return await self._repository.get_branch(tenant_id_val, "main")
        except OntologyNotFoundError:
            pass
        artifact = await self._repository.get_latest_base()
        self._validate(artifact.ontology)
        revision = OntologyRevision(
            tenant_id=tenant_id_val,
            revision_id=uuid4().hex,
            base_version=artifact.version,
            base_hash=artifact.content_hash,
            author=author,
            message=message,
        )
        branch = OntologyBranch(
            tenant_id=tenant_id_val,
            name="main",
            head_revision_id=revision.revision_id,
        )
        materialization = self._materialization(
            revision, artifact, OverlayAccumulator()
        )
        initialized = await self._repository.initialize_tenant(
            revision, branch, materialization
        )
        logger.info(
            "tenant_ontology_initialized",
            tenant_id=tenant_id_val,
            revision_id=initialized.head_revision_id,
            base_version=artifact.version,
        )
        return initialized

    async def materialize(
        self,
        tenant_id: str,
        revision_id: str | None = None,
        *,
        branch: str = "main",
    ) -> MaterializedOntology:
        """Returns or reconstructs one pinned, validated effective ontology."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        if revision_id is None:
            branch_ref = await self._repository.get_branch(
                tenant_id_val, branch
            )
            revision_id = branch_ref.head_revision_id
        cached = await self._repository.get_materialization(
            tenant_id_val, revision_id
        )
        if cached is not None:
            return cached
        revision = await self._repository.get_revision(
            tenant_id_val, revision_id
        )
        artifact = await self._repository.get_base(revision.base_version)
        if revision.parents:
            parent = await self.materialize(tenant_id_val, revision.parents[0])
            overlay = OverlayAccumulator.from_snapshot(parent.overlay)
        else:
            overlay = OverlayAccumulator()
        apply_changes(artifact.ontology, overlay, revision.changes)
        materialization = self._materialization(revision, artifact, overlay)
        await self._repository.put_materialization(materialization)
        return materialization

    async def create_branch(
        self,
        tenant_id: str,
        name: str,
        *,
        from_revision: str | None = None,
        from_branch: str = "main",
    ) -> OntologyBranch:
        """Creates a lightweight ref to an existing same-tenant revision."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        if from_revision is None:
            source = await self._repository.get_branch(
                tenant_id_val, from_branch
            )
            from_revision = source.head_revision_id
        await self._repository.get_revision(tenant_id_val, from_revision)
        branch = OntologyBranch(
            tenant_id=tenant_id_val,
            name=name,
            head_revision_id=from_revision,
        )
        await self._repository.create_branch(branch)
        logger.info(
            "tenant_ontology_branch_created",
            tenant_id=tenant_id_val,
            branch=name,
            revision_id=from_revision,
        )
        return branch

    async def _candidate_materialization(
        self,
        revision: OntologyRevision,
        parent: MaterializedOntology,
        artifact: BaseOntologyArtifact,
    ) -> MaterializedOntology:
        overlay = OverlayAccumulator.from_snapshot(parent.overlay)
        apply_changes(artifact.ontology, overlay, revision.changes)
        return self._materialization(revision, artifact, overlay)

    async def commit(
        self,
        tenant_id: str,
        branch: str,
        *,
        expected_head: str,
        changes: tuple[OntologyChange, ...],
        author: str,
        message: str,
    ) -> OntologyRevision:
        """Validates and atomically appends a normal one-parent revision."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        branch_ref = await self._repository.get_branch(tenant_id_val, branch)
        if branch_ref.head_revision_id != expected_head:
            raise ConcurrentHeadUpdateError(f"branch HEAD changed: {branch}")
        parent = await self._repository.get_revision(
            tenant_id_val, expected_head
        )
        parent_materialization = await self.materialize(
            tenant_id_val, expected_head
        )
        artifact = await self._repository.get_base(parent.base_version)
        revision = OntologyRevision(
            tenant_id=tenant_id_val,
            revision_id=uuid4().hex,
            base_version=parent.base_version,
            base_hash=parent.base_hash,
            parents=(parent.revision_id,),
            changes=changes,
            author=author,
            message=message,
        )
        materialization = await self._candidate_materialization(
            revision, parent_materialization, artifact
        )
        committed = await self._repository.commit_revision(
            branch, expected_head, revision, materialization
        )
        revision = committed or revision
        logger.info(
            "tenant_ontology_revision_committed",
            tenant_id=tenant_id_val,
            branch=branch,
            revision_id=revision.revision_id,
            change_count=len(changes),
        )
        return revision

    async def synchronize_base(
        self,
        tenant_id: str,
        branch: str,
        *,
        expected_head: str,
        author: str,
        message: str,
        target_version: str | None = None,
    ) -> OntologyRevision:
        """Replays the sparse overlay onto a new immutable platform artifact."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        branch_ref = await self._repository.get_branch(tenant_id_val, branch)
        if branch_ref.head_revision_id != expected_head:
            raise ConcurrentHeadUpdateError(f"branch HEAD changed: {branch}")
        parent = await self._repository.get_revision(
            tenant_id_val, expected_head
        )
        parent_materialization = await self.materialize(
            tenant_id_val, expected_head
        )
        artifact = (
            await self._repository.get_latest_base()
            if target_version is None
            else await self._repository.get_base(target_version)
        )
        target_resources = {
            resource.resource_id: resource
            for resource in artifact.ontology.resources
        }
        conflicts: list[MergeConflict] = []
        for override in parent_materialization.overlay.resources:
            target_resource = target_resources.get(override.resource_id)
            if override.added is not None and target_resource is not None:
                conflicts.append(
                    MergeConflict(
                        conflict_id=uuid4().hex,
                        kind=ConflictKind.ADD_ADD,
                        resource_id=override.resource_id,
                        base=ConflictValue(exists=False),
                        left=ConflictValue(
                            exists=True,
                            value=override.added.model_dump(mode="json"),
                        ),
                        right=ConflictValue(
                            exists=True,
                            value=target_resource.model_dump(mode="json"),
                        ),
                        message=(
                            "the new platform base allocated a tenant-owned "
                            "stable resource identifier"
                        ),
                    )
                )
            elif (
                override.added is None
                and override.fields
                and target_resource is None
            ):
                previous_resource = parent_materialization.ontology.get(
                    override.resource_id
                )
                conflicts.append(
                    MergeConflict(
                        conflict_id=uuid4().hex,
                        kind=ConflictKind.BASE_RESOURCE_REMOVED,
                        resource_id=override.resource_id,
                        base=ConflictValue(
                            exists=previous_resource is not None,
                            value=(
                                previous_resource.model_dump(mode="json")
                                if previous_resource is not None
                                else None
                            ),
                        ),
                        left=ConflictValue(
                            exists=True,
                            value={
                                "fields": [
                                    field.model_dump(mode="json")
                                    for field in override.fields
                                ]
                            },
                        ),
                        right=ConflictValue(exists=False),
                        message=(
                            "the new platform base removed a resource with "
                            "explicit tenant field overrides"
                        ),
                    )
                )
        if conflicts:
            raise BaseSynchronizationConflictError(tuple(conflicts))
        revision = OntologyRevision(
            tenant_id=tenant_id_val,
            revision_id=uuid4().hex,
            base_version=artifact.version,
            base_hash=artifact.content_hash,
            parents=(parent.revision_id,),
            author=author,
            message=message,
        )
        materialization = await self._candidate_materialization(
            revision, parent_materialization, artifact
        )
        committed = await self._repository.commit_revision(
            branch, expected_head, revision, materialization
        )
        revision = committed or revision
        logger.info(
            "tenant_ontology_base_synchronized",
            tenant_id=tenant_id_val,
            branch=branch,
            revision_id=revision.revision_id,
            base_version=artifact.version,
        )
        return revision

    async def merge_base(
        self, tenant_id: str, left_revision: str, right_revision: str
    ) -> str:
        """Finds the closest deterministic common ancestor in the tenant DAG."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        return await self._repository.find_merge_base(
            tenant_id_val, left_revision, right_revision
        )

    async def merge(
        self,
        tenant_id: str,
        *,
        target_branch: str,
        source_branch: str,
        expected_target_head: str,
        author: str,
        message: str,
    ) -> MergeResult:
        """Performs a validated semantic merge and preserves both parents."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        target = await self._repository.get_branch(tenant_id_val, target_branch)
        source = await self._repository.get_branch(tenant_id_val, source_branch)
        if target.head_revision_id != expected_target_head:
            raise ConcurrentHeadUpdateError(
                f"branch HEAD changed: {target_branch}"
            )
        left_revision = await self._repository.get_revision(
            tenant_id_val, target.head_revision_id
        )
        right_revision = await self._repository.get_revision(
            tenant_id_val, source.head_revision_id
        )
        if (
            left_revision.base_version != right_revision.base_version
            or left_revision.base_hash != right_revision.base_hash
        ):
            raise BaseVersionMismatchError(
                "branches must synchronize to one base artifact before merge"
            )
        base_revision_id = await self.merge_base(
            tenant_id_val,
            left_revision.revision_id,
            right_revision.revision_id,
        )
        base_materialization = await self.materialize(
            tenant_id_val, base_revision_id
        )
        left_materialization = await self.materialize(
            tenant_id_val, left_revision.revision_id
        )
        right_materialization = await self.materialize(
            tenant_id_val, right_revision.revision_id
        )
        merged, conflicts = three_way_merge(
            base_materialization.ontology,
            left_materialization.ontology,
            right_materialization.ontology,
            result_version=left_revision.base_version,
        )
        merge_id = uuid4().hex
        if conflicts:
            await self._repository.save_conflicts(
                tenant_id_val, merge_id, conflicts
            )
            logger.info(
                "tenant_ontology_merge_conflicted",
                tenant_id=tenant_id_val,
                target_branch=target_branch,
                source_branch=source_branch,
                merge_id=merge_id,
                conflict_count=len(conflicts),
            )
            return MergeResult(merge_id=merge_id, conflicts=conflicts)
        if merged is None:
            raise RuntimeError(
                "semantic merge returned neither result nor conflicts"
            )
        try:
            self._validate(merged)
        except OntologyValidationError as error:
            invalid_conflicts = tuple(
                MergeConflict(
                    conflict_id=uuid4().hex,
                    kind=ConflictKind.INVALID_RESULT,
                    resource_id=issue.resource_id or "core.ontology",
                    path=issue.path,
                    base=ConflictValue(exists=False),
                    left=ConflictValue(exists=False),
                    right=ConflictValue(exists=False),
                    message=issue.message,
                )
                for issue in error.issues
            )
            await self._repository.save_conflicts(
                tenant_id_val, merge_id, invalid_conflicts
            )
            return MergeResult(merge_id=merge_id, conflicts=invalid_conflicts)
        artifact = await self._repository.get_base(left_revision.base_version)
        desired_overlay = overlay_from_effective(artifact.ontology, merged)
        current_overlay = OverlayAccumulator.from_snapshot(
            left_materialization.overlay
        )
        changes = changes_between_overlays(current_overlay, desired_overlay)
        revision = OntologyRevision(
            tenant_id=tenant_id_val,
            revision_id=uuid4().hex,
            base_version=left_revision.base_version,
            base_hash=left_revision.base_hash,
            parents=(
                left_revision.revision_id,
                right_revision.revision_id,
            ),
            changes=changes,
            author=author,
            message=message,
        )
        materialization = await self._candidate_materialization(
            revision, left_materialization, artifact
        )
        committed = await self._repository.commit_revision(
            target_branch,
            expected_target_head,
            revision,
            materialization,
        )
        revision = committed or revision
        logger.info(
            "tenant_ontology_branches_merged",
            tenant_id=tenant_id_val,
            target_branch=target_branch,
            source_branch=source_branch,
            merge_id=merge_id,
            revision_id=revision.revision_id,
        )
        return MergeResult(merge_id=merge_id, revision=revision)

    async def diff(
        self, tenant_id: str, before_revision: str, after_revision: str
    ) -> tuple[OntologyChange, ...]:
        """Returns a semantic effective-ontology diff between tenant revisions."""
        before = await self.materialize(tenant_id, before_revision)
        after = await self.materialize(tenant_id, after_revision)
        return semantic_diff(before.ontology, after.ontology)
