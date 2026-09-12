from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Tenant(_message.Message):
    __slots__ = ("tenant_id", "head_revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    HEAD_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    head_revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., head_revision_id: _Optional[str] = ...) -> None: ...

class TenantValidation(_message.Message):
    __slots__ = ("tenant_id", "exists")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    EXISTS_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    exists: bool
    def __init__(self, tenant_id: _Optional[str] = ..., exists: bool = ...) -> None: ...

class ValidateTenantsRequest(_message.Message):
    __slots__ = ("tenant_ids",)
    TENANT_IDS_FIELD_NUMBER: _ClassVar[int]
    tenant_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, tenant_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class ValidateTenantsResponse(_message.Message):
    __slots__ = ("tenants",)
    TENANTS_FIELD_NUMBER: _ClassVar[int]
    tenants: _containers.RepeatedCompositeFieldContainer[TenantValidation]
    def __init__(self, tenants: _Optional[_Iterable[_Union[TenantValidation, _Mapping]]] = ...) -> None: ...

class GetTenantRequest(_message.Message):
    __slots__ = ("tenant_id",)
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    def __init__(self, tenant_id: _Optional[str] = ...) -> None: ...

class PutTenantRequest(_message.Message):
    __slots__ = ("tenant_id",)
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    def __init__(self, tenant_id: _Optional[str] = ...) -> None: ...

class DeleteTenantRequest(_message.Message):
    __slots__ = ("tenant_id", "confirmation_tenant_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIRMATION_TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    confirmation_tenant_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., confirmation_tenant_id: _Optional[str] = ...) -> None: ...

class DeleteTenantResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Pipeline(_message.Message):
    __slots__ = ("pipeline_id", "name", "owner_id", "head_revision_id", "published_revision_id", "definition_json", "author_id", "message", "created_at_ms", "updated_at_ms", "deleted_at_ms")
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    OWNER_ID_FIELD_NUMBER: _ClassVar[int]
    HEAD_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    DEFINITION_JSON_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    DELETED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    pipeline_id: str
    name: str
    owner_id: str
    head_revision_id: str
    published_revision_id: str
    definition_json: bytes
    author_id: str
    message: str
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int
    def __init__(self, pipeline_id: _Optional[str] = ..., name: _Optional[str] = ..., owner_id: _Optional[str] = ..., head_revision_id: _Optional[str] = ..., published_revision_id: _Optional[str] = ..., definition_json: _Optional[bytes] = ..., author_id: _Optional[str] = ..., message: _Optional[str] = ..., created_at_ms: _Optional[int] = ..., updated_at_ms: _Optional[int] = ..., deleted_at_ms: _Optional[int] = ...) -> None: ...

class PutPipelineRequest(_message.Message):
    __slots__ = ("tenant_id", "pipeline_id", "name", "owner_id", "definition_json", "author_id", "message", "expected_revision_id", "create_only")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    OWNER_ID_FIELD_NUMBER: _ClassVar[int]
    DEFINITION_JSON_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    CREATE_ONLY_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    pipeline_id: str
    name: str
    owner_id: str
    definition_json: bytes
    author_id: str
    message: str
    expected_revision_id: str
    create_only: bool
    def __init__(self, tenant_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., name: _Optional[str] = ..., owner_id: _Optional[str] = ..., definition_json: _Optional[bytes] = ..., author_id: _Optional[str] = ..., message: _Optional[str] = ..., expected_revision_id: _Optional[str] = ..., create_only: bool = ...) -> None: ...

class GetPipelineRequest(_message.Message):
    __slots__ = ("tenant_id", "pipeline_id", "revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    pipeline_id: str
    revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., revision_id: _Optional[str] = ...) -> None: ...

class ListPipelinesRequest(_message.Message):
    __slots__ = ("tenant_id", "limit")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    limit: int
    def __init__(self, tenant_id: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class ListPublishedPipelinesRequest(_message.Message):
    __slots__ = ("tenant_id", "limit")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    limit: int
    def __init__(self, tenant_id: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class ListPipelinesResponse(_message.Message):
    __slots__ = ("pipelines",)
    PIPELINES_FIELD_NUMBER: _ClassVar[int]
    pipelines: _containers.RepeatedCompositeFieldContainer[Pipeline]
    def __init__(self, pipelines: _Optional[_Iterable[_Union[Pipeline, _Mapping]]] = ...) -> None: ...

class PublishPipelineRequest(_message.Message):
    __slots__ = ("tenant_id", "pipeline_id", "revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    pipeline_id: str
    revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., revision_id: _Optional[str] = ...) -> None: ...

class DeletePipelineRequest(_message.Message):
    __slots__ = ("tenant_id", "pipeline_id", "expected_revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    pipeline_id: str
    expected_revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., expected_revision_id: _Optional[str] = ...) -> None: ...

class DeletePipelineResponse(_message.Message):
    __slots__ = ("revision_id",)
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    revision_id: str
    def __init__(self, revision_id: _Optional[str] = ...) -> None: ...

class GetRuntimePipelineRequest(_message.Message):
    __slots__ = ("tenant_id", "pipeline_id", "revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    pipeline_id: str
    revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., revision_id: _Optional[str] = ...) -> None: ...

class OntologyRevision(_message.Message):
    __slots__ = ("ontology_id", "display_name", "revision_id", "ontology_json", "author", "message", "created_at_ms")
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_JSON_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    ontology_id: str
    display_name: str
    revision_id: str
    ontology_json: bytes
    author: str
    message: str
    created_at_ms: int
    def __init__(self, ontology_id: _Optional[str] = ..., display_name: _Optional[str] = ..., revision_id: _Optional[str] = ..., ontology_json: _Optional[bytes] = ..., author: _Optional[str] = ..., message: _Optional[str] = ..., created_at_ms: _Optional[int] = ...) -> None: ...

class PutOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "display_name", "ontology_json", "author", "message", "expected_revision_id", "create_only")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_JSON_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    CREATE_ONLY_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    display_name: str
    ontology_json: bytes
    author: str
    message: str
    expected_revision_id: str
    create_only: bool
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., display_name: _Optional[str] = ..., ontology_json: _Optional[bytes] = ..., author: _Optional[str] = ..., message: _Optional[str] = ..., expected_revision_id: _Optional[str] = ..., create_only: bool = ...) -> None: ...

class GetOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., revision_id: _Optional[str] = ...) -> None: ...

class OntologyPublication(_message.Message):
    __slots__ = ("ontology_id", "display_name", "publication_id", "revision_id", "lifecycle", "metadata_json", "base_version", "base_hash", "effective_hash", "author", "message", "published_at_ms", "retired_at_ms")
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    PUBLICATION_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_FIELD_NUMBER: _ClassVar[int]
    METADATA_JSON_FIELD_NUMBER: _ClassVar[int]
    BASE_VERSION_FIELD_NUMBER: _ClassVar[int]
    BASE_HASH_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_HASH_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    RETIRED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    ontology_id: str
    display_name: str
    publication_id: str
    revision_id: str
    lifecycle: str
    metadata_json: bytes
    base_version: str
    base_hash: str
    effective_hash: str
    author: str
    message: str
    published_at_ms: int
    retired_at_ms: int
    def __init__(self, ontology_id: _Optional[str] = ..., display_name: _Optional[str] = ..., publication_id: _Optional[str] = ..., revision_id: _Optional[str] = ..., lifecycle: _Optional[str] = ..., metadata_json: _Optional[bytes] = ..., base_version: _Optional[str] = ..., base_hash: _Optional[str] = ..., effective_hash: _Optional[str] = ..., author: _Optional[str] = ..., message: _Optional[str] = ..., published_at_ms: _Optional[int] = ..., retired_at_ms: _Optional[int] = ...) -> None: ...

class PublishOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "display_name", "publication_id", "revision_id", "metadata_json", "create_only")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    PUBLICATION_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    METADATA_JSON_FIELD_NUMBER: _ClassVar[int]
    CREATE_ONLY_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    display_name: str
    publication_id: str
    revision_id: str
    metadata_json: bytes
    create_only: bool
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., display_name: _Optional[str] = ..., publication_id: _Optional[str] = ..., revision_id: _Optional[str] = ..., metadata_json: _Optional[bytes] = ..., create_only: bool = ...) -> None: ...

class RetireOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "publication_id", "revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    PUBLICATION_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    publication_id: str
    revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., publication_id: _Optional[str] = ..., revision_id: _Optional[str] = ...) -> None: ...

class RetireOntologyResponse(_message.Message):
    __slots__ = ("head_revision_id",)
    HEAD_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    head_revision_id: str
    def __init__(self, head_revision_id: _Optional[str] = ...) -> None: ...

class ListOntologiesRequest(_message.Message):
    __slots__ = ("tenant_id", "limit")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    limit: int
    def __init__(self, tenant_id: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class OntologyCatalogEntry(_message.Message):
    __slots__ = ("ontology_id", "display_name", "production_publication")
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    PRODUCTION_PUBLICATION_FIELD_NUMBER: _ClassVar[int]
    ontology_id: str
    display_name: str
    production_publication: OntologyPublication
    def __init__(self, ontology_id: _Optional[str] = ..., display_name: _Optional[str] = ..., production_publication: _Optional[_Union[OntologyPublication, _Mapping]] = ...) -> None: ...

class ListOntologiesResponse(_message.Message):
    __slots__ = ("ontologies",)
    ONTOLOGIES_FIELD_NUMBER: _ClassVar[int]
    ontologies: _containers.RepeatedCompositeFieldContainer[OntologyCatalogEntry]
    def __init__(self, ontologies: _Optional[_Iterable[_Union[OntologyCatalogEntry, _Mapping]]] = ...) -> None: ...

class OntologyBinding(_message.Message):
    __slots__ = ("pipeline_id", "block_id", "ontology_id", "resource_ids", "resource_kinds", "include_dependencies", "metadata_json", "updated_at_ms", "head_revision_id")
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDS_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_KINDS_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_DEPENDENCIES_FIELD_NUMBER: _ClassVar[int]
    METADATA_JSON_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    HEAD_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    pipeline_id: str
    block_id: str
    ontology_id: str
    resource_ids: _containers.RepeatedScalarFieldContainer[str]
    resource_kinds: _containers.RepeatedScalarFieldContainer[str]
    include_dependencies: bool
    metadata_json: bytes
    updated_at_ms: int
    head_revision_id: str
    def __init__(self, pipeline_id: _Optional[str] = ..., block_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., resource_ids: _Optional[_Iterable[str]] = ..., resource_kinds: _Optional[_Iterable[str]] = ..., include_dependencies: bool = ..., metadata_json: _Optional[bytes] = ..., updated_at_ms: _Optional[int] = ..., head_revision_id: _Optional[str] = ...) -> None: ...

class PutBindingRequest(_message.Message):
    __slots__ = ("tenant_id", "binding", "expected_revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    BINDING_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    binding: OntologyBinding
    expected_revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., binding: _Optional[_Union[OntologyBinding, _Mapping]] = ..., expected_revision_id: _Optional[str] = ...) -> None: ...

class ListBindingsRequest(_message.Message):
    __slots__ = ("tenant_id", "pipeline_id", "limit", "revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    pipeline_id: str
    limit: int
    revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., limit: _Optional[int] = ..., revision_id: _Optional[str] = ...) -> None: ...

class ListBindingsResponse(_message.Message):
    __slots__ = ("bindings",)
    BINDINGS_FIELD_NUMBER: _ClassVar[int]
    bindings: _containers.RepeatedCompositeFieldContainer[OntologyBinding]
    def __init__(self, bindings: _Optional[_Iterable[_Union[OntologyBinding, _Mapping]]] = ...) -> None: ...

class GetOntologySliceRequest(_message.Message):
    __slots__ = ("tenant_id", "pipeline_id", "pipeline_revision_id", "block_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    pipeline_id: str
    pipeline_revision_id: str
    block_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., pipeline_revision_id: _Optional[str] = ..., block_id: _Optional[str] = ...) -> None: ...

class OntologySlice(_message.Message):
    __slots__ = ("ontology_id", "publication_id", "revision_id", "ontology_json", "publication_metadata_json", "binding_metadata_json", "published_at_ms", "base_version", "base_hash", "effective_hash")
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    PUBLICATION_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_JSON_FIELD_NUMBER: _ClassVar[int]
    PUBLICATION_METADATA_JSON_FIELD_NUMBER: _ClassVar[int]
    BINDING_METADATA_JSON_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    BASE_VERSION_FIELD_NUMBER: _ClassVar[int]
    BASE_HASH_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_HASH_FIELD_NUMBER: _ClassVar[int]
    ontology_id: str
    publication_id: str
    revision_id: str
    ontology_json: bytes
    publication_metadata_json: bytes
    binding_metadata_json: bytes
    published_at_ms: int
    base_version: str
    base_hash: str
    effective_hash: str
    def __init__(self, ontology_id: _Optional[str] = ..., publication_id: _Optional[str] = ..., revision_id: _Optional[str] = ..., ontology_json: _Optional[bytes] = ..., publication_metadata_json: _Optional[bytes] = ..., binding_metadata_json: _Optional[bytes] = ..., published_at_ms: _Optional[int] = ..., base_version: _Optional[str] = ..., base_hash: _Optional[str] = ..., effective_hash: _Optional[str] = ...) -> None: ...

class DiffOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "base_revision_id", "target_revision_id", "ontology_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    BASE_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    base_revision_id: str
    target_revision_id: str
    ontology_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., base_revision_id: _Optional[str] = ..., target_revision_id: _Optional[str] = ..., ontology_id: _Optional[str] = ...) -> None: ...

class SemanticChange(_message.Message):
    __slots__ = ("resource_id", "operation", "path", "before_json", "after_json")
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    BEFORE_JSON_FIELD_NUMBER: _ClassVar[int]
    AFTER_JSON_FIELD_NUMBER: _ClassVar[int]
    resource_id: str
    operation: str
    path: _containers.RepeatedScalarFieldContainer[str]
    before_json: bytes
    after_json: bytes
    def __init__(self, resource_id: _Optional[str] = ..., operation: _Optional[str] = ..., path: _Optional[_Iterable[str]] = ..., before_json: _Optional[bytes] = ..., after_json: _Optional[bytes] = ...) -> None: ...

class SemanticDiff(_message.Message):
    __slots__ = ("changes",)
    CHANGES_FIELD_NUMBER: _ClassVar[int]
    changes: _containers.RepeatedCompositeFieldContainer[SemanticChange]
    def __init__(self, changes: _Optional[_Iterable[_Union[SemanticChange, _Mapping]]] = ...) -> None: ...

class MergeOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "base_revision_id", "left_revision_id", "right_revision_id", "author", "message", "expected_revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    BASE_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    LEFT_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    RIGHT_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    base_revision_id: str
    left_revision_id: str
    right_revision_id: str
    author: str
    message: str
    expected_revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., base_revision_id: _Optional[str] = ..., left_revision_id: _Optional[str] = ..., right_revision_id: _Optional[str] = ..., author: _Optional[str] = ..., message: _Optional[str] = ..., expected_revision_id: _Optional[str] = ...) -> None: ...

class MergeConflict(_message.Message):
    __slots__ = ("resource_id", "path", "base_json", "left_json", "right_json")
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    BASE_JSON_FIELD_NUMBER: _ClassVar[int]
    LEFT_JSON_FIELD_NUMBER: _ClassVar[int]
    RIGHT_JSON_FIELD_NUMBER: _ClassVar[int]
    resource_id: str
    path: _containers.RepeatedScalarFieldContainer[str]
    base_json: bytes
    left_json: bytes
    right_json: bytes
    def __init__(self, resource_id: _Optional[str] = ..., path: _Optional[_Iterable[str]] = ..., base_json: _Optional[bytes] = ..., left_json: _Optional[bytes] = ..., right_json: _Optional[bytes] = ...) -> None: ...

class OntologyMerge(_message.Message):
    __slots__ = ("revision", "conflicts")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CONFLICTS_FIELD_NUMBER: _ClassVar[int]
    revision: OntologyRevision
    conflicts: _containers.RepeatedCompositeFieldContainer[MergeConflict]
    def __init__(self, revision: _Optional[_Union[OntologyRevision, _Mapping]] = ..., conflicts: _Optional[_Iterable[_Union[MergeConflict, _Mapping]]] = ...) -> None: ...
