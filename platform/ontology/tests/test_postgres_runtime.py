"""PostgreSQL runtime publication and slice extraction tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from galadril_ontology import (
    BaseOntologyArtifact,
    ConflictKind,
    ConflictValue,
    MaterializedOntology,
    MergeConflict,
    Ontology,
    OntologyBranch,
    OntologyChange,
    OntologyResource,
    OntologyRevision,
    OntologySliceRequest,
    OverlaySnapshot,
    PipelineOntologyBinding,
    PublishedOntology,
    ResourceKind,
)
from galadril_ontology.errors import (
    BranchAlreadyExistsError,
    ConcurrentHeadUpdateError,
    OntologyNotFoundError,
)
from galadril_ontology.model import ontology_content_hash
from galadril_ontology.postgres import (
    LOAD_RUNTIME_ONTOLOGY_SLICE_SQL,
    ONTOLOGY_SCHEMA_SQL,
    PostgresConnectionProvider,
    PostgresOntologyRepository,
)
from galadril_ontology.runtime import OntologySliceSelector
from psycopg.errors import UniqueViolation


class _Cursor:
    __slots__ = ("_row", "_rows", "rowcount")

    def __init__(
        self,
        row: tuple[object, ...] | None,
        rowcount: int = 1,
        rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or []
        self.rowcount = rowcount

    async def fetchone(self) -> tuple[object, ...] | None:
        return self._row

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _Connection:
    __slots__ = ("calls", "results", "row")

    def __init__(
        self,
        row: tuple[object, ...] | None = None,
        *,
        results: list[_Cursor | BaseException] | None = None,
    ) -> None:
        self.row = row
        self.results = results or []
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(
        self, query: str, parameters: tuple[object, ...] = ()
    ) -> _Cursor:
        self.calls.append((query, parameters))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return _Cursor(self.row)


class _Provider:
    __slots__ = ("connection",)

    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def tenant_connection(
        self, tenant_id: str
    ) -> AsyncIterator[_Connection]:
        yield self.connection

    @asynccontextmanager
    async def maintenance_connection(self) -> AsyncIterator[_Connection]:
        yield self.connection


def _ontology() -> Ontology:
    return Ontology(
        version="vision-1",
        resources=(
            OntologyResource(
                resource_id="core.customer",
                kind=ResourceKind.OBJECT_TYPE,
                display_name="Customer",
            ),
        ),
    )


def _artifact() -> BaseOntologyArtifact:
    return BaseOntologyArtifact.from_ontology(_ontology())


def _revision(*, parents: tuple[str, ...] = ()) -> OntologyRevision:
    artifact = _artifact()
    return OntologyRevision(
        tenant_id="tenant-a",
        revision_id="1" * 32,
        base_version=artifact.version,
        base_hash=artifact.content_hash,
        parents=parents,
        changes=(
            OntologyChange.set_field(
                "core.customer", ("description",), "Tenant description"
            ),
        ),
        author="test",
        message="test revision",
    )


def _materialization() -> MaterializedOntology:
    ontology = _ontology()
    artifact = _artifact()
    return MaterializedOntology(
        tenant_id="tenant-a",
        revision_id="1" * 32,
        base_version=artifact.version,
        base_hash=artifact.content_hash,
        effective_hash=ontology_content_hash(ontology),
        overlay=OverlaySnapshot(),
        ontology=ontology,
    )


def _repository(connection: _Connection) -> PostgresOntologyRepository:
    return PostgresOntologyRepository(
        cast(PostgresConnectionProvider, _Provider(connection))
    )


def test_runtime_schema_enforces_catalog_publication_and_binding_isolation() -> (
    None
):
    """Keeps all mutable runtime pointers inside tenant-keyed PostgreSQL rows."""
    assert "CREATE TABLE IF NOT EXISTS ontology_catalog" in ONTOLOGY_SCHEMA_SQL
    assert (
        "CREATE TABLE IF NOT EXISTS ontology_publications"
        in ONTOLOGY_SCHEMA_SQL
    )
    assert (
        "CREATE TABLE IF NOT EXISTS pipeline_ontology_bindings"
        in ONTOLOGY_SCHEMA_SQL
    )
    assert (
        "UNIQUE (tenant_id, ontology_id, publication_id)" in ONTOLOGY_SCHEMA_SQL
    )
    assert "WHERE lifecycle = 'production'" in ONTOLOGY_SCHEMA_SQL
    assert "FOREIGN KEY (tenant_id, ontology_id)" in ONTOLOGY_SCHEMA_SQL
    assert "pipeline_ontology_bindings" in ONTOLOGY_SCHEMA_SQL
    assert "WITH RECURSIVE" in LOAD_RUNTIME_ONTOLOGY_SLICE_SQL
    assert "jsonb_array_elements" in LOAD_RUNTIME_ONTOLOGY_SLICE_SQL


@pytest.mark.asyncio
async def test_postgres_loads_only_bound_production_slice_and_metadata() -> (
    None
):
    """Deserializes one SQL-filtered slice without loading a full ontology."""
    published_at = datetime(2026, 8, 25, tzinfo=UTC)
    connection = _Connection(
        (
            "2" * 32,
            "sales",
            "1" * 32,
            "vision-1",
            "a" * 64,
            "b" * 64,
            {"release": "stable"},
            {"purpose": "identity"},
            published_at,
            "vision-1",
            [
                {
                    "resource_id": "core.customer",
                    "kind": "object_type",
                    "display_name": "Customer",
                    "description": "",
                    "owner_id": None,
                    "value_type": None,
                    "references": [],
                    "attributes": {},
                }
            ],
        )
    )
    repository = PostgresOntologyRepository(
        cast(PostgresConnectionProvider, _Provider(connection))
    )

    ontology_slice = await repository.load_runtime_slice(
        OntologySliceRequest(
            tenant_id="tenant-a",
            pipeline_id="pipeline-a",
            block_id="resolve",
        )
    )

    assert ontology_slice.metadata.ontology_id == "sales"
    assert ontology_slice.metadata.publication_metadata == {"release": "stable"}
    assert (
        ontology_slice.ontology.require("core.customer").kind
        is ResourceKind.OBJECT_TYPE
    )
    assert connection.calls == [
        (
            LOAD_RUNTIME_ONTOLOGY_SLICE_SQL,
            ("tenant-a", "pipeline-a", "resolve"),
        )
    ]


@pytest.mark.asyncio
async def test_postgres_upserts_pipeline_binding() -> None:
    """Serializes semantic selectors through a tenant RLS transaction."""
    connection = _Connection()
    repository = PostgresOntologyRepository(
        cast(PostgresConnectionProvider, _Provider(connection))
    )
    binding = PipelineOntologyBinding(
        tenant_id="tenant-a",
        pipeline_id="pipeline-a",
        block_id="sink",
        ontology_id="sales",
        selector=OntologySliceSelector(
            resource_ids=("core.customer",),
            kinds=(ResourceKind.PROPERTY,),
            include_dependencies=False,
        ),
        metadata={"purpose": "persistence"},
    )

    await repository.bind(binding)

    query, parameters = connection.calls[0]
    assert "INSERT INTO pipeline_ontology_bindings" in query
    assert parameters[:4] == ("tenant-a", "pipeline-a", "sink", "sales")


@pytest.mark.asyncio
async def test_postgres_schema_and_base_artifact_lifecycle() -> None:
    """Covers privileged schema and immutable base artifact persistence paths."""
    artifact = _artifact()
    connection = _Connection(results=[_Cursor(None)])
    repository = _repository(connection)
    await repository.initialize_schema()
    assert connection.calls[0][0] == ONTOLOGY_SCHEMA_SQL

    connection.results = [_Cursor(None, rowcount=1)]
    await repository.register_base(artifact)
    connection.results = [
        _Cursor(None, rowcount=0),
        _Cursor((artifact.content_hash,)),
    ]
    await repository.register_base(artifact)
    connection.results = [_Cursor(None, rowcount=0), _Cursor(None)]
    with pytest.raises(ValueError, match="different content"):
        await repository.register_base(artifact)


@pytest.mark.asyncio
async def test_postgres_extracts_base_revisions_and_branches() -> None:
    """Deserializes canonical history and fails closed for absent rows."""
    artifact = _artifact()
    created_at = datetime(2026, 8, 25, tzinfo=UTC)
    base_row = (
        artifact.version,
        artifact.content_hash,
        artifact.ontology.model_dump_json().encode(),
        created_at,
    )
    connection = _Connection(results=[_Cursor(base_row), _Cursor(base_row)])
    repository = _repository(connection)
    assert await repository.get_base(artifact.version) == artifact.model_copy(
        update={"created_at": created_at}
    )
    assert (
        await repository.get_latest_base()
    ).content_hash == artifact.content_hash

    connection.results = [_Cursor(None)]
    with pytest.raises(OntologyNotFoundError, match="base ontology"):
        await repository.get_base("missing")

    revision = _revision(parents=("0" * 32,))
    change_payload = [
        change.model_dump(mode="json") for change in revision.changes
    ]
    connection.results = [
        _Cursor(
            (
                revision.base_version,
                revision.base_hash,
                change_payload,
                revision.author,
                revision.message,
                revision.created_at,
            )
        ),
        _Cursor(None, rows=[("0" * 32,)]),
        _Cursor((revision.revision_id,)),
    ]
    assert (
        await repository.get_revision("tenant-a", revision.revision_id)
        == revision
    )
    assert (
        await repository.get_branch("tenant-a", "main")
    ).head_revision_id == revision.revision_id

    connection.results = [_Cursor(None)]
    with pytest.raises(OntologyNotFoundError, match="revision"):
        await repository.get_revision("tenant-a", "f" * 32)
    connection.results = [
        _Cursor(
            (
                revision.base_version,
                revision.base_hash,
                {},
                revision.author,
                revision.message,
                revision.created_at,
            )
        ),
        _Cursor(None, rows=[]),
    ]
    with pytest.raises(ValueError, match="not an array"):
        await repository.get_revision("tenant-a", revision.revision_id)
    connection.results = [_Cursor(None)]
    with pytest.raises(OntologyNotFoundError, match="branch"):
        await repository.get_branch("tenant-a", "missing")


@pytest.mark.asyncio
async def test_postgres_initializes_and_mutates_revision_graph_safely() -> None:
    """Exercises idempotent roots, branches, merge bases, and branch CAS."""
    revision = _revision()
    branch = OntologyBranch(
        tenant_id="tenant-a",
        name="main",
        head_revision_id=revision.revision_id,
    )
    materialization = _materialization()
    connection = _Connection(
        results=[_Cursor(None), _Cursor((revision.revision_id,))]
    )
    repository = _repository(connection)
    assert (
        await repository.initialize_tenant(revision, branch, materialization)
    ) == branch

    connection.results = [_Cursor(None), _Cursor(None)]
    assert (
        await repository.initialize_tenant(revision, branch, materialization)
    ) == branch

    connection.results = [_Cursor(None)]
    await repository.create_branch(
        branch.model_copy(update={"name": "experiment"})
    )
    connection.results = [UniqueViolation("duplicate")]
    with pytest.raises(BranchAlreadyExistsError):
        await repository.create_branch(
            branch.model_copy(update={"name": "experiment"})
        )

    connection.results = [_Cursor((revision.revision_id,))]
    assert (
        await repository.find_merge_base(
            "tenant-a", revision.revision_id, revision.revision_id
        )
        == revision.revision_id
    )
    connection.results = [_Cursor(None)]
    with pytest.raises(OntologyNotFoundError, match="common ancestor"):
        await repository.find_merge_base(
            "tenant-a", revision.revision_id, "f" * 32
        )

    child = revision.model_copy(
        update={"revision_id": "2" * 32, "parents": (revision.revision_id,)}
    )
    child_materialization = materialization.model_copy(
        update={"revision_id": child.revision_id}
    )
    connection.results = [
        _Cursor(None),
        _Cursor(None),
        _Cursor(None),
        _Cursor(None, rowcount=1),
    ]
    await repository.commit_revision(
        "main", revision.revision_id, child, child_materialization
    )
    connection.results = [
        _Cursor(None),
        _Cursor(None),
        _Cursor(None),
        _Cursor(None, rowcount=0),
    ]
    with pytest.raises(ConcurrentHeadUpdateError):
        await repository.commit_revision(
            "main", revision.revision_id, child, child_materialization
        )


@pytest.mark.asyncio
async def test_postgres_materialization_conflicts_and_publications() -> None:
    """Covers disposable cache, structured conflict, and production promotion IO."""
    materialization = _materialization()
    connection = _Connection(results=[_Cursor(None)])
    repository = _repository(connection)
    assert await repository.get_materialization("tenant-a", "f" * 32) is None

    connection.results = [
        _Cursor(
            (
                materialization.base_version,
                materialization.base_hash,
                materialization.effective_hash,
                materialization.overlay.model_dump_json(),
                materialization.ontology.model_dump_json(),
            )
        )
    ]
    assert (
        await repository.get_materialization(
            "tenant-a", materialization.revision_id
        )
        == materialization
    )
    connection.results = [_Cursor(None)]
    await repository.put_materialization(materialization)

    conflict = MergeConflict(
        conflict_id="3" * 32,
        kind=ConflictKind.FIELD_VALUE,
        resource_id="core.customer",
        path=("description",),
        base=ConflictValue(exists=True, value="base"),
        left=ConflictValue(exists=True, value="left"),
        right=ConflictValue(exists=True, value="right"),
        message="conflict",
    )
    connection.results = [_Cursor(None)]
    await repository.save_conflicts("tenant-a", "4" * 32, (conflict,))
    connection.results = [
        _Cursor(None, rows=[(conflict.model_dump_json().encode(),)])
    ]
    assert await repository.list_conflicts("tenant-a", "4" * 32) == (conflict,)

    publication = PublishedOntology(
        tenant_id="tenant-a",
        ontology_id="sales",
        publication_id="5" * 32,
        materialization=materialization,
        metadata={"display_name": "Sales Ontology"},
        published_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    connection.results = [_Cursor(None), _Cursor(None), _Cursor(None)]
    await repository.publish(publication)
    assert "Sales Ontology" in connection.calls[-3][1]

    connection.results = [_Cursor(None)]
    with pytest.raises(OntologyNotFoundError, match="production ontology"):
        await repository.load_runtime_slice(
            OntologySliceRequest(
                tenant_id="tenant-a",
                pipeline_id="missing",
                block_id="missing",
            )
        )
