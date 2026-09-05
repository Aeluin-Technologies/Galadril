"""Persistence contracts and a deterministic in-memory reference adapter."""

from __future__ import annotations

import asyncio
from typing import Protocol

from galadril_ontology.errors import (
    BranchAlreadyExistsError,
    ConcurrentHeadUpdateError,
    OntologyNotFoundError,
)
from galadril_ontology.identity import normalize_tenant_id
from galadril_ontology.model import (
    BaseOntologyArtifact,
    MaterializedOntology,
    MergeConflict,
    OntologyBranch,
    OntologyRevision,
)


class OntologyRepository(Protocol):
    """Defines the single authoritative consistency boundary for history."""

    async def register_base(self, artifact: BaseOntologyArtifact) -> None: ...

    async def get_base(self, version: str) -> BaseOntologyArtifact: ...

    async def get_latest_base(self) -> BaseOntologyArtifact: ...

    async def initialize_tenant(
        self,
        revision: OntologyRevision,
        branch: OntologyBranch,
        materialization: MaterializedOntology,
    ) -> OntologyBranch: ...

    async def get_revision(
        self, tenant_id: str, revision_id: str
    ) -> OntologyRevision: ...

    async def get_branch(self, tenant_id: str, name: str) -> OntologyBranch: ...

    async def create_branch(self, branch: OntologyBranch) -> None: ...

    async def find_merge_base(
        self, tenant_id: str, left_revision: str, right_revision: str
    ) -> str: ...

    async def commit_revision(
        self,
        branch_name: str,
        expected_head: str,
        revision: OntologyRevision,
        materialization: MaterializedOntology,
    ) -> OntologyRevision | None: ...

    async def get_materialization(
        self, tenant_id: str, revision_id: str
    ) -> MaterializedOntology | None: ...

    async def put_materialization(
        self, materialization: MaterializedOntology
    ) -> None: ...

    async def save_conflicts(
        self,
        tenant_id: str,
        merge_id: str,
        conflicts: tuple[MergeConflict, ...],
    ) -> None: ...

    async def list_conflicts(
        self, tenant_id: str, merge_id: str
    ) -> tuple[MergeConflict, ...]: ...


class InMemoryOntologyRepository:
    """Exercises production invariants without pretending to be durable."""

    __slots__ = (
        "_bases",
        "_branches",
        "_conflicts",
        "_latest_base_version",
        "_lock",
        "_materializations",
        "_materialization_write_count",
        "_revisions",
    )

    def __init__(self) -> None:
        self._bases: dict[str, BaseOntologyArtifact] = {}
        self._latest_base_version: str | None = None
        self._revisions: dict[tuple[str, str], OntologyRevision] = {}
        self._branches: dict[tuple[str, str], OntologyBranch] = {}
        self._materializations: dict[tuple[str, str], MaterializedOntology] = {}
        self._conflicts: dict[tuple[str, str], tuple[MergeConflict, ...]] = {}
        self._materialization_write_count = 0
        self._lock = asyncio.Lock()

    @property
    def materialization_write_count(self) -> int:
        """Exposes cache writes for deterministic cache behavior tests."""
        return self._materialization_write_count

    async def register_base(self, artifact: BaseOntologyArtifact) -> None:
        """Registers one global artifact and rejects release-name reuse."""
        async with self._lock:
            current = self._bases.get(artifact.version)
            if (
                current is not None
                and current.content_hash != artifact.content_hash
            ):
                raise ValueError(
                    f"base version already has different content: {artifact.version}"
                )
            self._bases[artifact.version] = artifact
            self._latest_base_version = artifact.version

    async def get_base(self, version: str) -> BaseOntologyArtifact:
        artifact = self._bases.get(version)
        if artifact is None:
            raise OntologyNotFoundError(
                f"base ontology artifact is unavailable: {version}"
            )
        return artifact

    async def get_latest_base(self) -> BaseOntologyArtifact:
        version = self._latest_base_version
        if version is None:
            raise OntologyNotFoundError(
                "no base ontology artifact is registered"
            )
        return self._bases[version]

    async def initialize_tenant(
        self,
        revision: OntologyRevision,
        branch: OntologyBranch,
        materialization: MaterializedOntology,
    ) -> OntologyBranch:
        tenant_id = normalize_tenant_id(revision.tenant_id)
        key = (tenant_id, branch.name)
        async with self._lock:
            current = self._branches.get(key)
            if current is not None:
                return current
            self._validate_revision_parents(revision)
            self._revisions[(tenant_id, revision.revision_id)] = revision
            self._branches[key] = branch
            self._store_materialization(materialization)
            return branch

    async def get_revision(
        self, tenant_id: str, revision_id: str
    ) -> OntologyRevision:
        tenant_id_val = normalize_tenant_id(tenant_id)
        revision = self._revisions.get((tenant_id_val, revision_id))
        if revision is None:
            raise OntologyNotFoundError("tenant revision is unavailable")
        return revision

    async def get_branch(self, tenant_id: str, name: str) -> OntologyBranch:
        tenant_id_val = normalize_tenant_id(tenant_id)
        branch = self._branches.get((tenant_id_val, name))
        if branch is None:
            raise OntologyNotFoundError("tenant branch is unavailable")
        return branch

    async def create_branch(self, branch: OntologyBranch) -> None:
        tenant_id = normalize_tenant_id(branch.tenant_id)
        key = (tenant_id, branch.name)
        async with self._lock:
            if key in self._branches:
                raise BranchAlreadyExistsError(
                    f"branch already exists: {branch.name}"
                )
            if (tenant_id, branch.head_revision_id) not in self._revisions:
                raise OntologyNotFoundError("tenant revision is unavailable")
            self._branches[key] = branch

    async def _ancestor_distances(
        self, tenant_id: str, revision_id: str
    ) -> dict[str, int]:
        distances: dict[str, int] = {}
        pending: list[tuple[str, int]] = [(revision_id, 0)]
        while pending:
            current, distance = pending.pop()
            previous = distances.get(current)
            if previous is not None and previous <= distance:
                continue
            revision = self._revisions.get((tenant_id, current))
            if revision is None:
                raise OntologyNotFoundError("tenant revision is unavailable")
            distances[current] = distance
            pending.extend(
                (parent, distance + 1) for parent in revision.parents
            )
        return distances

    async def find_merge_base(
        self, tenant_id: str, left_revision: str, right_revision: str
    ) -> str:
        """Finds the closest common ancestor within one tenant namespace."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        left = await self._ancestor_distances(tenant_id_val, left_revision)
        right = await self._ancestor_distances(tenant_id_val, right_revision)
        common = set(left) & set(right)
        if not common:
            raise OntologyNotFoundError("revisions have no common ancestor")
        return min(
            common,
            key=lambda item: (
                max(left[item], right[item]),
                left[item] + right[item],
                item,
            ),
        )

    def _validate_revision_parents(self, revision: OntologyRevision) -> None:
        for parent in revision.parents:
            if (revision.tenant_id, parent) not in self._revisions:
                raise OntologyNotFoundError(
                    "tenant parent revision is unavailable"
                )

    async def commit_revision(
        self,
        branch_name: str,
        expected_head: str,
        revision: OntologyRevision,
        materialization: MaterializedOntology,
    ) -> None:
        tenant_id = normalize_tenant_id(revision.tenant_id)
        key = (tenant_id, branch_name)
        async with self._lock:
            branch = self._branches.get(key)
            if branch is None:
                raise OntologyNotFoundError("tenant branch is unavailable")
            if branch.head_revision_id != expected_head:
                raise ConcurrentHeadUpdateError(
                    f"branch HEAD changed: {branch_name}"
                )
            self._validate_revision_parents(revision)
            revision_key = (tenant_id, revision.revision_id)
            if revision_key in self._revisions:
                raise ValueError("revision identifier already exists")
            self._revisions[revision_key] = revision
            self._store_materialization(materialization)
            self._branches[key] = branch.model_copy(
                update={"head_revision_id": revision.revision_id}
            )

    async def get_materialization(
        self, tenant_id: str, revision_id: str
    ) -> MaterializedOntology | None:
        tenant_id_val = normalize_tenant_id(tenant_id)
        return self._materializations.get((tenant_id_val, revision_id))

    def _store_materialization(
        self, materialization: MaterializedOntology
    ) -> None:
        key = (materialization.tenant_id, materialization.revision_id)
        if key not in self._materializations:
            self._materialization_write_count += 1
        self._materializations[key] = materialization

    async def put_materialization(
        self, materialization: MaterializedOntology
    ) -> None:
        async with self._lock:
            self._store_materialization(materialization)

    async def save_conflicts(
        self,
        tenant_id: str,
        merge_id: str,
        conflicts: tuple[MergeConflict, ...],
    ) -> None:
        tenant_id_val = normalize_tenant_id(tenant_id)
        async with self._lock:
            self._conflicts[(tenant_id_val, merge_id)] = conflicts

    async def list_conflicts(
        self, tenant_id: str, merge_id: str
    ) -> tuple[MergeConflict, ...]:
        tenant_id_val = normalize_tenant_id(tenant_id)
        return self._conflicts.get((tenant_id_val, merge_id), ())
