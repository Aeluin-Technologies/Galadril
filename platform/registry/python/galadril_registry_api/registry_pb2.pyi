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

class ListTenantsRequest(_message.Message):
    __slots__ = ("limit", "page_token")
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    limit: int
    page_token: str
    def __init__(self, limit: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class ListTenantsResponse(_message.Message):
    __slots__ = ("tenant_ids", "next_page_token")
    TENANT_IDS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    tenant_ids: _containers.RepeatedScalarFieldContainer[str]
    next_page_token: str
    def __init__(self, tenant_ids: _Optional[_Iterable[str]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

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
    __slots__ = ("pipeline_id", "name", "owner_id", "head_revision_id", "published_revision_id", "definition_json", "author_id", "message", "created_at_ms", "updated_at_ms", "deleted_at_ms", "previous_revision_id", "authoring_json")
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
    PREVIOUS_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    AUTHORING_JSON_FIELD_NUMBER: _ClassVar[int]
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
    previous_revision_id: str
    authoring_json: bytes
    def __init__(self, pipeline_id: _Optional[str] = ..., name: _Optional[str] = ..., owner_id: _Optional[str] = ..., head_revision_id: _Optional[str] = ..., published_revision_id: _Optional[str] = ..., definition_json: _Optional[bytes] = ..., author_id: _Optional[str] = ..., message: _Optional[str] = ..., created_at_ms: _Optional[int] = ..., updated_at_ms: _Optional[int] = ..., deleted_at_ms: _Optional[int] = ..., previous_revision_id: _Optional[str] = ..., authoring_json: _Optional[bytes] = ...) -> None: ...

class PutPipelineRequest(_message.Message):
    __slots__ = ("tenant_id", "pipeline_id", "name", "owner_id", "definition_json", "author_id", "message", "expected_revision_id", "create_only", "authoring_command_json")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    OWNER_ID_FIELD_NUMBER: _ClassVar[int]
    DEFINITION_JSON_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    CREATE_ONLY_FIELD_NUMBER: _ClassVar[int]
    AUTHORING_COMMAND_JSON_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    pipeline_id: str
    name: str
    owner_id: str
    definition_json: bytes
    author_id: str
    message: str
    expected_revision_id: str
    create_only: bool
    authoring_command_json: bytes
    def __init__(self, tenant_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., name: _Optional[str] = ..., owner_id: _Optional[str] = ..., definition_json: _Optional[bytes] = ..., author_id: _Optional[str] = ..., message: _Optional[str] = ..., expected_revision_id: _Optional[str] = ..., create_only: bool = ..., authoring_command_json: _Optional[bytes] = ...) -> None: ...

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

class GolFile(_message.Message):
    __slots__ = ("path", "text")
    PATH_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    path: str
    text: str
    def __init__(self, path: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...

class GolDiagnostic(_message.Message):
    __slots__ = ("code", "message", "path", "start", "end", "severity")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    SEVERITY_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    path: str
    start: int
    end: int
    severity: str
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ..., path: _Optional[str] = ..., start: _Optional[int] = ..., end: _Optional[int] = ..., severity: _Optional[str] = ...) -> None: ...

class GolDependency(_message.Message):
    __slots__ = ("ontology_id", "generation", "source_revision")
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    ontology_id: str
    generation: str
    source_revision: str
    def __init__(self, ontology_id: _Optional[str] = ..., generation: _Optional[str] = ..., source_revision: _Optional[str] = ...) -> None: ...

class GolPackageOrigin(_message.Message):
    __slots__ = ("plugin_id", "plugin_version", "package_version")
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_VERSION_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    plugin_id: str
    plugin_version: str
    package_version: str
    def __init__(self, plugin_id: _Optional[str] = ..., plugin_version: _Optional[str] = ..., package_version: _Optional[str] = ...) -> None: ...

class GolSummary(_message.Message):
    __slots__ = ("ontology_id", "display_name", "source_revision", "active_generation", "active_source_revision", "status", "origin")
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    ontology_id: str
    display_name: str
    source_revision: str
    active_generation: str
    active_source_revision: str
    status: str
    origin: GolPackageOrigin
    def __init__(self, ontology_id: _Optional[str] = ..., display_name: _Optional[str] = ..., source_revision: _Optional[str] = ..., active_generation: _Optional[str] = ..., active_source_revision: _Optional[str] = ..., status: _Optional[str] = ..., origin: _Optional[_Union[GolPackageOrigin, _Mapping]] = ...) -> None: ...

class GolWorkspace(_message.Message):
    __slots__ = ("summary", "files", "diagnostics", "dependencies")
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    DIAGNOSTICS_FIELD_NUMBER: _ClassVar[int]
    DEPENDENCIES_FIELD_NUMBER: _ClassVar[int]
    summary: GolSummary
    files: _containers.RepeatedCompositeFieldContainer[GolFile]
    diagnostics: _containers.RepeatedCompositeFieldContainer[GolDiagnostic]
    dependencies: _containers.RepeatedCompositeFieldContainer[GolDependency]
    def __init__(self, summary: _Optional[_Union[GolSummary, _Mapping]] = ..., files: _Optional[_Iterable[_Union[GolFile, _Mapping]]] = ..., diagnostics: _Optional[_Iterable[_Union[GolDiagnostic, _Mapping]]] = ..., dependencies: _Optional[_Iterable[_Union[GolDependency, _Mapping]]] = ...) -> None: ...

class GolValidation(_message.Message):
    __slots__ = ("ontology_id", "source_revision", "status", "diagnostics", "dependencies")
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DIAGNOSTICS_FIELD_NUMBER: _ClassVar[int]
    DEPENDENCIES_FIELD_NUMBER: _ClassVar[int]
    ontology_id: str
    source_revision: str
    status: str
    diagnostics: _containers.RepeatedCompositeFieldContainer[GolDiagnostic]
    dependencies: _containers.RepeatedCompositeFieldContainer[GolDependency]
    def __init__(self, ontology_id: _Optional[str] = ..., source_revision: _Optional[str] = ..., status: _Optional[str] = ..., diagnostics: _Optional[_Iterable[_Union[GolDiagnostic, _Mapping]]] = ..., dependencies: _Optional[_Iterable[_Union[GolDependency, _Mapping]]] = ...) -> None: ...

class GolPublication(_message.Message):
    __slots__ = ("ontology_id", "source_revision", "generation")
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    ontology_id: str
    source_revision: str
    generation: str
    def __init__(self, ontology_id: _Optional[str] = ..., source_revision: _Optional[str] = ..., generation: _Optional[str] = ...) -> None: ...

class SaveGolOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "display_name", "files", "author", "expected_revision", "origin")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    display_name: str
    files: _containers.RepeatedCompositeFieldContainer[GolFile]
    author: str
    expected_revision: str
    origin: GolPackageOrigin
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., display_name: _Optional[str] = ..., files: _Optional[_Iterable[_Union[GolFile, _Mapping]]] = ..., author: _Optional[str] = ..., expected_revision: _Optional[str] = ..., origin: _Optional[_Union[GolPackageOrigin, _Mapping]] = ...) -> None: ...

class GetGolWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "source_revision")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    source_revision: str
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., source_revision: _Optional[str] = ...) -> None: ...

class ValidateGolOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "source_revision", "plan_pin", "approved_owners")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    PLAN_PIN_FIELD_NUMBER: _ClassVar[int]
    APPROVED_OWNERS_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    source_revision: str
    plan_pin: str
    approved_owners: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., source_revision: _Optional[str] = ..., plan_pin: _Optional[str] = ..., approved_owners: _Optional[_Iterable[str]] = ...) -> None: ...

class PublishGolOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "source_revision", "expected_generation", "author_id", "plan_pin", "approved_owners")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_GENERATION_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_ID_FIELD_NUMBER: _ClassVar[int]
    PLAN_PIN_FIELD_NUMBER: _ClassVar[int]
    APPROVED_OWNERS_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    source_revision: str
    expected_generation: str
    author_id: str
    plan_pin: str
    approved_owners: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., source_revision: _Optional[str] = ..., expected_generation: _Optional[str] = ..., author_id: _Optional[str] = ..., plan_pin: _Optional[str] = ..., approved_owners: _Optional[_Iterable[str]] = ...) -> None: ...

class PreviewGolDependenciesRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "source_revision", "approved_owners")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    APPROVED_OWNERS_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    source_revision: str
    approved_owners: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., source_revision: _Optional[str] = ..., approved_owners: _Optional[_Iterable[str]] = ...) -> None: ...

class GolDependencyPreview(_message.Message):
    __slots__ = ("required_owners", "plan_pin", "dependencies")
    REQUIRED_OWNERS_FIELD_NUMBER: _ClassVar[int]
    PLAN_PIN_FIELD_NUMBER: _ClassVar[int]
    DEPENDENCIES_FIELD_NUMBER: _ClassVar[int]
    required_owners: _containers.RepeatedScalarFieldContainer[str]
    plan_pin: str
    dependencies: _containers.RepeatedCompositeFieldContainer[GolDependency]
    def __init__(self, required_owners: _Optional[_Iterable[str]] = ..., plan_pin: _Optional[str] = ..., dependencies: _Optional[_Iterable[_Union[GolDependency, _Mapping]]] = ...) -> None: ...

class DeleteGolOntologyRequest(_message.Message):
    __slots__ = ("tenant_id", "ontology_id", "expected_revision", "author")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    ontology_id: str
    expected_revision: str
    author: str
    def __init__(self, tenant_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., expected_revision: _Optional[str] = ..., author: _Optional[str] = ...) -> None: ...

class DeleteGolOntologyResponse(_message.Message):
    __slots__ = ("revision",)
    REVISION_FIELD_NUMBER: _ClassVar[int]
    revision: str
    def __init__(self, revision: _Optional[str] = ...) -> None: ...

class ListGolOntologiesRequest(_message.Message):
    __slots__ = ("tenant_id", "limit")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    limit: int
    def __init__(self, tenant_id: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class ListGolOntologiesResponse(_message.Message):
    __slots__ = ("ontologies",)
    ONTOLOGIES_FIELD_NUMBER: _ClassVar[int]
    ontologies: _containers.RepeatedCompositeFieldContainer[GolSummary]
    def __init__(self, ontologies: _Optional[_Iterable[_Union[GolSummary, _Mapping]]] = ...) -> None: ...

class GolBinding(_message.Message):
    __slots__ = ("pipeline_id", "block_id", "ontology_id", "generation", "source_revision", "symbols", "updated_at_ms", "head_revision_id")
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    ONTOLOGY_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    SYMBOLS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    HEAD_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    pipeline_id: str
    block_id: str
    ontology_id: str
    generation: str
    source_revision: str
    symbols: _containers.RepeatedScalarFieldContainer[str]
    updated_at_ms: int
    head_revision_id: str
    def __init__(self, pipeline_id: _Optional[str] = ..., block_id: _Optional[str] = ..., ontology_id: _Optional[str] = ..., generation: _Optional[str] = ..., source_revision: _Optional[str] = ..., symbols: _Optional[_Iterable[str]] = ..., updated_at_ms: _Optional[int] = ..., head_revision_id: _Optional[str] = ...) -> None: ...

class PutBindingRequest(_message.Message):
    __slots__ = ("tenant_id", "binding", "expected_revision_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    BINDING_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    binding: GolBinding
    expected_revision_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., binding: _Optional[_Union[GolBinding, _Mapping]] = ..., expected_revision_id: _Optional[str] = ...) -> None: ...

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
    bindings: _containers.RepeatedCompositeFieldContainer[GolBinding]
    def __init__(self, bindings: _Optional[_Iterable[_Union[GolBinding, _Mapping]]] = ...) -> None: ...

class GetGolBindingRequest(_message.Message):
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
