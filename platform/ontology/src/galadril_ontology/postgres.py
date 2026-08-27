"""PostgreSQL source-of-truth adapter for ontology revision history."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol, cast

import orjson
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import JsonValue, TypeAdapter

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
    Ontology,
    OntologyBranch,
    OntologyChange,
    OntologyRevision,
    OverlaySnapshot,
)
from galadril_ontology.runtime import (
    OntologySlice,
    OntologySliceMetadata,
    OntologySliceRequest,
    PipelineOntologyBinding,
    PublishedOntology,
)
from galadril_ontology.schema import postgres_schema_sql

LOAD_RUNTIME_ONTOLOGY_SLICE_SQL = """
WITH RECURSIVE runtime_context AS (
    SELECT
        publication.publication_id,
        publication.ontology_id,
        publication.revision_id,
        materialization.base_version,
        materialization.base_hash,
        materialization.effective_hash,
        publication.metadata AS publication_metadata,
        binding.metadata AS binding_metadata,
        publication.published_at,
        materialization.effective_ontology,
        binding.resource_ids,
        binding.resource_kinds,
        binding.include_dependencies
    FROM pipeline_ontology_bindings AS binding
    JOIN ontology_publications AS publication
      ON publication.tenant_id = binding.tenant_id
     AND publication.ontology_id = binding.ontology_id
     AND publication.lifecycle = 'production'
    JOIN ontology_materializations AS materialization
      ON materialization.tenant_id = publication.tenant_id
     AND materialization.revision_id = publication.revision_id
    WHERE binding.tenant_id = %s
      AND binding.pipeline_id = %s
      AND binding.block_id = %s
), all_resources AS (
    SELECT resource
    FROM runtime_context AS context
    CROSS JOIN LATERAL jsonb_array_elements(
        context.effective_ontology->'resources'
    ) AS resource
), selected_ids(resource_id) AS (
    SELECT resource->>'resource_id'
    FROM all_resources
    CROSS JOIN runtime_context AS context
    WHERE context.resource_ids ? (resource->>'resource_id')
       OR context.resource_kinds ? (resource->>'kind')
    UNION
    SELECT dependency.resource_id
    FROM selected_ids AS selected
    JOIN all_resources AS current
      ON current.resource->>'resource_id' = selected.resource_id
    CROSS JOIN runtime_context AS context
    CROSS JOIN LATERAL jsonb_array_elements_text(
        COALESCE(current.resource->'references', '[]'::jsonb)
        || CASE
            WHEN current.resource->>'owner_id' IS NULL THEN '[]'::jsonb
            ELSE jsonb_build_array(current.resource->>'owner_id')
           END
    ) AS dependency(resource_id)
    WHERE context.include_dependencies
), selected_resources AS (
    SELECT resources.resource
    FROM all_resources AS resources
    JOIN selected_ids AS selected
      ON selected.resource_id = resources.resource->>'resource_id'
)
SELECT
    context.publication_id,
    context.ontology_id,
    context.revision_id,
    context.base_version,
    context.base_hash,
    context.effective_hash,
    context.publication_metadata,
    context.binding_metadata,
    context.published_at,
    context.effective_ontology->>'version',
    COALESCE(
        jsonb_agg(
            selected_resources.resource
            ORDER BY selected_resources.resource->>'resource_id'
        ) FILTER (WHERE selected_resources.resource IS NOT NULL),
        '[]'::jsonb
    )
FROM runtime_context AS context
LEFT JOIN selected_resources ON TRUE
GROUP BY
    context.publication_id,
    context.ontology_id,
    context.revision_id,
    context.base_version,
    context.base_hash,
    context.effective_hash,
    context.publication_metadata,
    context.binding_metadata,
    context.published_at,
    context.effective_ontology;
"""

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class PostgresConnectionProvider(Protocol):
    """Supplies established tenant and maintenance transaction boundaries."""

    def tenant_connection(
        self, tenant_id: str
    ) -> AbstractAsyncContextManager[AsyncConnection[tuple[object, ...]]]: ...

    def maintenance_connection(
        self,
    ) -> AbstractAsyncContextManager[AsyncConnection[tuple[object, ...]]]: ...


def _decode_json(value: object) -> object:
    if isinstance(value, bytes | bytearray | memoryview | str):
        return orjson.loads(value)
    return value


def _changes_payload(
    changes: tuple[OntologyChange, ...],
) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], change.model_dump(mode="json"))
        for change in changes
    ]


class PostgresOntologyRepository:
    """Persists canonical ontology history in tenant-isolated relational rows."""

    __slots__ = ("_connections",)

    def __init__(self, connections: PostgresConnectionProvider) -> None:
        self._connections = connections

    async def initialize_schema(self) -> None:
        """Applies idempotent Ontology and shared extension resources."""
        async with self._connections.maintenance_connection() as connection:
            for statement in postgres_schema_sql():
                await connection.execute(statement)

    async def register_base(self, artifact: BaseOntologyArtifact) -> None:
        """Stores one shared artifact through the privileged platform path."""
        async with self._connections.maintenance_connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO ontology_base_artifacts (
                    base_version, base_hash, ontology, created_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (base_version) DO NOTHING
                """,
                (
                    artifact.version,
                    artifact.content_hash,
                    Jsonb(artifact.ontology.model_dump(mode="json")),
                    artifact.created_at,
                ),
            )
            if cursor.rowcount == 1:
                return
            current = await connection.execute(
                """
                SELECT base_hash
                FROM ontology_base_artifacts
                WHERE base_version = %s
                """,
                (artifact.version,),
            )
            row = await current.fetchone()
            if row is None or str(row[0]) != artifact.content_hash:
                raise ValueError(
                    f"base version already has different content: {artifact.version}"
                )

    async def _get_base_on_connection(
        self,
        connection: AsyncConnection[tuple[object, ...]],
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> BaseOntologyArtifact:
        cursor = await connection.execute(query, parameters)
        row = await cursor.fetchone()
        if row is None:
            raise OntologyNotFoundError("base ontology artifact is unavailable")
        ontology = Ontology.model_validate(_decode_json(row[2]))
        return BaseOntologyArtifact(
            version=str(row[0]),
            content_hash=str(row[1]),
            ontology=ontology,
            created_at=cast(datetime, row[3]),
        )

    async def get_base(self, version: str) -> BaseOntologyArtifact:
        async with self._connections.tenant_connection(
            "galadril-system"
        ) as connection:
            return await self._get_base_on_connection(
                connection,
                """
                SELECT base_version, base_hash, ontology, created_at
                FROM ontology_base_artifacts
                WHERE base_version = %s
                """,
                (version,),
            )

    async def get_latest_base(self) -> BaseOntologyArtifact:
        async with self._connections.tenant_connection(
            "galadril-system"
        ) as connection:
            return await self._get_base_on_connection(
                connection,
                """
                SELECT base_version, base_hash, ontology, created_at
                FROM ontology_base_artifacts
                ORDER BY created_at DESC, base_version DESC
                LIMIT 1
                """,
            )

    async def _insert_revision(
        self,
        connection: AsyncConnection[tuple[object, ...]],
        revision: OntologyRevision,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO ontology_revisions (
                tenant_id, revision_id, base_version, base_hash,
                change_set, author, message, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                revision.tenant_id,
                revision.revision_id,
                revision.base_version,
                revision.base_hash,
                Jsonb(_changes_payload(revision.changes)),
                revision.author,
                revision.message,
                revision.created_at,
            ),
        )
        for parent_order, parent_revision_id in enumerate(revision.parents):
            await connection.execute(
                """
                INSERT INTO ontology_revision_parents (
                    tenant_id, revision_id, parent_revision_id, parent_order
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    revision.tenant_id,
                    revision.revision_id,
                    parent_revision_id,
                    parent_order,
                ),
            )

    async def _insert_materialization(
        self,
        connection: AsyncConnection[tuple[object, ...]],
        materialization: MaterializedOntology,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO ontology_materializations (
                tenant_id, revision_id, base_version, base_hash,
                effective_hash, overlay, effective_ontology
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, revision_id) DO NOTHING
            """,
            (
                materialization.tenant_id,
                materialization.revision_id,
                materialization.base_version,
                materialization.base_hash,
                materialization.effective_hash,
                Jsonb(materialization.overlay.model_dump(mode="json")),
                Jsonb(materialization.ontology.model_dump(mode="json")),
            ),
        )

    async def initialize_tenant(
        self,
        revision: OntologyRevision,
        branch: OntologyBranch,
        materialization: MaterializedOntology,
    ) -> OntologyBranch:
        tenant_id = normalize_tenant_id(revision.tenant_id)
        async with self._connections.tenant_connection(tenant_id) as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"ontology:{tenant_id}:main",),
            )
            cursor = await connection.execute(
                """
                SELECT head_revision_id
                FROM ontology_branches
                WHERE tenant_id = %s AND branch_name = 'main'
                """,
                (tenant_id,),
            )
            row = await cursor.fetchone()
            if row is not None:
                return OntologyBranch(
                    tenant_id=tenant_id,
                    name="main",
                    head_revision_id=str(row[0]),
                )
            await self._insert_revision(connection, revision)
            await self._insert_materialization(connection, materialization)
            await connection.execute(
                """
                INSERT INTO ontology_branches (
                    tenant_id, branch_name, head_revision_id
                ) VALUES (%s, %s, %s)
                """,
                (tenant_id, branch.name, branch.head_revision_id),
            )
            return branch

    async def get_revision(
        self, tenant_id: str, revision_id: str
    ) -> OntologyRevision:
        tenant_id_val = normalize_tenant_id(tenant_id)
        async with self._connections.tenant_connection(
            tenant_id_val
        ) as connection:
            cursor = await connection.execute(
                """
                SELECT base_version, base_hash, change_set, author,
                       message, created_at
                FROM ontology_revisions
                WHERE tenant_id = %s AND revision_id = %s
                """,
                (tenant_id_val, revision_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise OntologyNotFoundError("tenant revision is unavailable")
            parents_cursor = await connection.execute(
                """
                SELECT parent_revision_id
                FROM ontology_revision_parents
                WHERE tenant_id = %s AND revision_id = %s
                ORDER BY parent_order
                """,
                (tenant_id_val, revision_id),
            )
            parent_rows = await parents_cursor.fetchall()
        raw_changes = _decode_json(row[2])
        if not isinstance(raw_changes, list):
            raise ValueError("stored ontology change set is not an array")
        changes = tuple(
            OntologyChange.model_validate(item) for item in raw_changes
        )
        return OntologyRevision(
            tenant_id=tenant_id_val,
            revision_id=revision_id,
            base_version=str(row[0]),
            base_hash=str(row[1]),
            parents=tuple(str(parent[0]) for parent in parent_rows),
            changes=changes,
            author=str(row[3]),
            message=str(row[4]),
            created_at=cast(datetime, row[5]),
        )

    async def get_branch(self, tenant_id: str, name: str) -> OntologyBranch:
        tenant_id_val = normalize_tenant_id(tenant_id)
        async with self._connections.tenant_connection(
            tenant_id_val
        ) as connection:
            cursor = await connection.execute(
                """
                SELECT head_revision_id
                FROM ontology_branches
                WHERE tenant_id = %s AND branch_name = %s
                """,
                (tenant_id_val, name),
            )
            row = await cursor.fetchone()
        if row is None:
            raise OntologyNotFoundError("tenant branch is unavailable")
        return OntologyBranch(
            tenant_id=tenant_id_val,
            name=name,
            head_revision_id=str(row[0]),
        )

    async def create_branch(self, branch: OntologyBranch) -> None:
        tenant_id = normalize_tenant_id(branch.tenant_id)
        try:
            async with self._connections.tenant_connection(
                tenant_id
            ) as connection:
                await connection.execute(
                    """
                    INSERT INTO ontology_branches (
                        tenant_id, branch_name, head_revision_id
                    ) VALUES (%s, %s, %s)
                    """,
                    (tenant_id, branch.name, branch.head_revision_id),
                )
        except UniqueViolation as error:
            raise BranchAlreadyExistsError(
                f"branch already exists: {branch.name}"
            ) from error

    async def find_merge_base(
        self, tenant_id: str, left_revision: str, right_revision: str
    ) -> str:
        """Uses recursive relational traversal for the closest merge base."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        async with self._connections.tenant_connection(
            tenant_id_val
        ) as connection:
            cursor = await connection.execute(
                """
                WITH RECURSIVE
                left_walk(revision_id, depth) AS (
                    SELECT revision_id, 0
                    FROM ontology_revisions
                    WHERE tenant_id = %s AND revision_id = %s
                    UNION ALL
                    SELECT parent.parent_revision_id, walk.depth + 1
                    FROM left_walk AS walk
                    JOIN ontology_revision_parents AS parent
                      ON parent.tenant_id = %s
                     AND parent.revision_id = walk.revision_id
                ),
                right_walk(revision_id, depth) AS (
                    SELECT revision_id, 0
                    FROM ontology_revisions
                    WHERE tenant_id = %s AND revision_id = %s
                    UNION ALL
                    SELECT parent.parent_revision_id, walk.depth + 1
                    FROM right_walk AS walk
                    JOIN ontology_revision_parents AS parent
                      ON parent.tenant_id = %s
                     AND parent.revision_id = walk.revision_id
                ),
                left_ancestors AS (
                    SELECT revision_id, MIN(depth) AS depth
                    FROM left_walk GROUP BY revision_id
                ),
                right_ancestors AS (
                    SELECT revision_id, MIN(depth) AS depth
                    FROM right_walk GROUP BY revision_id
                )
                SELECT left_side.revision_id
                FROM left_ancestors AS left_side
                JOIN right_ancestors AS right_side USING (revision_id)
                ORDER BY GREATEST(left_side.depth, right_side.depth),
                         left_side.depth + right_side.depth,
                         left_side.revision_id
                LIMIT 1
                """,
                (
                    tenant_id_val,
                    left_revision,
                    tenant_id_val,
                    tenant_id_val,
                    right_revision,
                    tenant_id_val,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise OntologyNotFoundError(
                "revisions have no tenant-scoped common ancestor"
            )
        return str(row[0])

    async def commit_revision(
        self,
        branch_name: str,
        expected_head: str,
        revision: OntologyRevision,
        materialization: MaterializedOntology,
    ) -> None:
        tenant_id = normalize_tenant_id(revision.tenant_id)
        async with self._connections.tenant_connection(tenant_id) as connection:
            await self._insert_revision(connection, revision)
            await self._insert_materialization(connection, materialization)
            cursor = await connection.execute(
                """
                UPDATE ontology_branches
                SET head_revision_id = %s, updated_at = NOW()
                WHERE tenant_id = %s AND branch_name = %s
                  AND head_revision_id IS NOT DISTINCT FROM %s
                """,
                (
                    revision.revision_id,
                    tenant_id,
                    branch_name,
                    expected_head,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentHeadUpdateError(
                    f"branch HEAD changed: {branch_name}"
                )

    async def get_materialization(
        self, tenant_id: str, revision_id: str
    ) -> MaterializedOntology | None:
        tenant_id_val = normalize_tenant_id(tenant_id)
        async with self._connections.tenant_connection(
            tenant_id_val
        ) as connection:
            cursor = await connection.execute(
                """
                SELECT base_version, base_hash, effective_hash,
                       overlay, effective_ontology
                FROM ontology_materializations
                WHERE tenant_id = %s AND revision_id = %s
                """,
                (tenant_id_val, revision_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return MaterializedOntology(
            tenant_id=tenant_id_val,
            revision_id=revision_id,
            base_version=str(row[0]),
            base_hash=str(row[1]),
            effective_hash=str(row[2]),
            overlay=OverlaySnapshot.model_validate(_decode_json(row[3])),
            ontology=Ontology.model_validate(_decode_json(row[4])),
        )

    async def put_materialization(
        self, materialization: MaterializedOntology
    ) -> None:
        async with self._connections.tenant_connection(
            materialization.tenant_id
        ) as connection:
            await self._insert_materialization(connection, materialization)

    async def save_conflicts(
        self,
        tenant_id: str,
        merge_id: str,
        conflicts: tuple[MergeConflict, ...],
    ) -> None:
        tenant_id_val = normalize_tenant_id(tenant_id)
        async with self._connections.tenant_connection(
            tenant_id_val
        ) as connection:
            for conflict_order, conflict in enumerate(conflicts):
                await connection.execute(
                    """
                    INSERT INTO ontology_merge_conflicts (
                        tenant_id, merge_id, conflict_id,
                        conflict_order, conflict
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id_val,
                        merge_id,
                        conflict.conflict_id,
                        conflict_order,
                        Jsonb(conflict.model_dump(mode="json")),
                    ),
                )

    async def list_conflicts(
        self, tenant_id: str, merge_id: str
    ) -> tuple[MergeConflict, ...]:
        tenant_id_val = normalize_tenant_id(tenant_id)
        async with self._connections.tenant_connection(
            tenant_id_val
        ) as connection:
            cursor = await connection.execute(
                """
                SELECT conflict
                FROM ontology_merge_conflicts
                WHERE tenant_id = %s AND merge_id = %s
                ORDER BY conflict_order
                """,
                (tenant_id_val, merge_id),
            )
            rows = await cursor.fetchall()
        return tuple(
            MergeConflict.model_validate(_decode_json(row[0])) for row in rows
        )

    async def publish(self, publication: PublishedOntology) -> None:
        """Promotes one revision as the sole production ontology atomically."""
        tenant_id = normalize_tenant_id(publication.tenant_id)
        display_name_value = publication.metadata.get("display_name")
        display_name = (
            display_name_value
            if isinstance(display_name_value, str) and display_name_value
            else publication.ontology_id
        )
        async with self._connections.tenant_connection(tenant_id) as connection:
            await connection.execute(
                """
                INSERT INTO ontology_catalog (
                    tenant_id, ontology_id, display_name
                ) VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id, ontology_id) DO UPDATE
                SET display_name = EXCLUDED.display_name
                """,
                (tenant_id, publication.ontology_id, display_name),
            )
            await connection.execute(
                """
                UPDATE ontology_publications
                SET lifecycle = 'retired', retired_at = %s
                WHERE tenant_id = %s AND ontology_id = %s
                  AND lifecycle = 'production'
                """,
                (publication.published_at, tenant_id, publication.ontology_id),
            )
            await connection.execute(
                """
                INSERT INTO ontology_publications (
                    tenant_id, ontology_id, publication_id, revision_id,
                    lifecycle, metadata, published_at
                ) VALUES (%s, %s, %s, %s, 'production', %s, %s)
                """,
                (
                    tenant_id,
                    publication.ontology_id,
                    publication.publication_id,
                    publication.materialization.revision_id,
                    Jsonb(publication.metadata),
                    publication.published_at,
                ),
            )

    async def bind(self, binding: PipelineOntologyBinding) -> None:
        """Upserts one block selector without copying ontology resources."""
        tenant_id = normalize_tenant_id(binding.tenant_id)
        async with self._connections.tenant_connection(tenant_id) as connection:
            await connection.execute(
                """
                INSERT INTO pipeline_ontology_bindings (
                    tenant_id, pipeline_id, block_id, ontology_id,
                    resource_ids, resource_kinds, include_dependencies, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, pipeline_id, block_id) DO UPDATE SET
                    ontology_id = EXCLUDED.ontology_id,
                    resource_ids = EXCLUDED.resource_ids,
                    resource_kinds = EXCLUDED.resource_kinds,
                    include_dependencies = EXCLUDED.include_dependencies,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                (
                    tenant_id,
                    binding.pipeline_id,
                    binding.block_id,
                    binding.ontology_id,
                    Jsonb(list(binding.selector.resource_ids)),
                    Jsonb([kind.value for kind in binding.selector.kinds]),
                    binding.selector.include_dependencies,
                    Jsonb(binding.metadata),
                ),
            )

    async def load_runtime_slice(
        self, request: OntologySliceRequest
    ) -> OntologySlice:
        """Loads only the SQL-selected production resources for one block."""
        tenant_id = normalize_tenant_id(request.tenant_id)
        async with self._connections.tenant_connection(tenant_id) as connection:
            cursor = await connection.execute(
                LOAD_RUNTIME_ONTOLOGY_SLICE_SQL,
                (tenant_id, request.pipeline_id, request.block_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise OntologyNotFoundError(
                "pipeline block production ontology is unavailable"
            )
        ontology = Ontology.model_validate(
            {
                "version": str(row[9]),
                "resources": _decode_json(row[10]),
            }
        )
        return OntologySlice(
            metadata=OntologySliceMetadata(
                tenant_id=tenant_id,
                ontology_id=str(row[1]),
                publication_id=str(row[0]),
                revision_id=str(row[2]),
                base_version=str(row[3]),
                base_hash=str(row[4]),
                effective_hash=str(row[5]),
                publication_metadata=_JSON_OBJECT.validate_python(
                    _decode_json(row[6])
                ),
                binding_metadata=_JSON_OBJECT.validate_python(
                    _decode_json(row[7])
                ),
                published_at=TypeAdapter(datetime).validate_python(row[8]),
            ),
            ontology=ontology,
        )
