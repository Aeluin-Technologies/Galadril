"""Published ontology requirements enforced before invoking step handlers."""

from typing import cast

from galadril_ontology import GolBlockContract, GolSymbolKind
from galadril_pipeline.config import PipelineStep

from galadril_vision.application.errors import CommandProcessingError


def ontology_contract(step: PipelineStep) -> GolBlockContract:
    """Compiles explicit block requirements with safe step-kind defaults."""
    parameters = step.params.model_extra or {}
    if any(key.startswith("ontology_") for key in parameters):
        raise CommandProcessingError(
            "Legacy ontology parameters are unsupported"
        )
    required_value = parameters.get("gol_required_symbols", ())
    allowed_value = parameters.get("gol_allowed_kinds")
    if not isinstance(required_value, list | tuple) or not all(
        isinstance(item, str) for item in required_value
    ):
        raise CommandProcessingError(
            "gol_required_symbols must be an array of resource identifiers"
        )
    if allowed_value is None:
        allowed_kinds = tuple(GolSymbolKind)
    elif isinstance(allowed_value, list | tuple) and all(
        isinstance(item, str) for item in allowed_value
    ):
        try:
            allowed_kinds = tuple(GolSymbolKind(item) for item in allowed_value)
        except ValueError as error:
            raise CommandProcessingError(
                "gol_allowed_kinds contains an unsupported resource kind"
            ) from error
    else:
        raise CommandProcessingError(
            "gol_allowed_kinds must be an array of resource kinds"
        )
    state_key = parameters.get("gol_state_symbol_key")
    derived_key = parameters.get("gol_derived_symbol_key")
    transfer_key = parameters.get("gol_transfer_symbol_key")
    if any(
        value is not None and not isinstance(value, str)
        for value in (state_key, derived_key, transfer_key)
    ):
        raise CommandProcessingError(
            "GOL effect targets must be symbol selectors"
        )
    return GolBlockContract(
        state_symbol_key=cast(str | None, state_key),
        derived_symbol_key=cast(str | None, derived_key),
        required_symbol_keys=tuple(
            cast(list[str] | tuple[str, ...], required_value)
        )
        + ((transfer_key,) if isinstance(transfer_key, str) else ()),
        allowed_kinds=allowed_kinds,
    )
