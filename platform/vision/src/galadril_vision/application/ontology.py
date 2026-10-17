"""Published ontology requirements enforced before invoking step handlers."""

from typing import cast

from galadril_ontology import BlockOntologyContract, ResourceKind
from galadril_pipeline.config import PipelineStep, StepType

from galadril_vision.application.errors import CommandProcessingError

_DEFAULT_ONTOLOGY_KINDS: dict[StepType, tuple[ResourceKind, ...]] = {
    StepType.INFERENCE: tuple(ResourceKind),
    StepType.RESOLVE: (
        ResourceKind.OBJECT_TYPE,
        ResourceKind.PROPERTY,
        ResourceKind.LINK_TYPE,
    ),
    StepType.SINK: (
        ResourceKind.OBJECT_TYPE,
        ResourceKind.EVENT_TYPE,
        ResourceKind.PROPERTY,
        ResourceKind.LINK_TYPE,
    ),
    StepType.DBT: (
        ResourceKind.OBJECT_TYPE,
        ResourceKind.PROPERTY,
        ResourceKind.LINK_TYPE,
    ),
    StepType.CAUSAL: (
        ResourceKind.OBJECT_TYPE,
        ResourceKind.EVENT_TYPE,
        ResourceKind.PROPERTY,
        ResourceKind.LINK_TYPE,
    ),
}


def ontology_contract(step: PipelineStep) -> BlockOntologyContract:
    """Compiles explicit block requirements with safe step-kind defaults."""
    parameters = step.params.model_extra or {}
    required_value = parameters.get("ontology_required_resources", ())
    allowed_value = parameters.get("ontology_allowed_kinds")
    if not isinstance(required_value, list | tuple) or not all(
        isinstance(item, str) for item in required_value
    ):
        raise CommandProcessingError(
            "ontology_required_resources must be an array of resource identifiers"
        )
    if allowed_value is None:
        allowed_kinds = _DEFAULT_ONTOLOGY_KINDS[step.type]
    elif isinstance(allowed_value, list | tuple) and all(
        isinstance(item, str) for item in allowed_value
    ):
        try:
            allowed_kinds = tuple(ResourceKind(item) for item in allowed_value)
        except ValueError as error:
            raise CommandProcessingError(
                "ontology_allowed_kinds contains an unsupported resource kind"
            ) from error
    else:
        raise CommandProcessingError(
            "ontology_allowed_kinds must be an array of resource kinds"
        )
    return BlockOntologyContract(
        required_resource_ids=tuple(
            cast(list[str] | tuple[str, ...], required_value)
        ),
        allowed_kinds=allowed_kinds,
    )
