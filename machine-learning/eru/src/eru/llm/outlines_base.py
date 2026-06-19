"""Base class tracking LLM interface utilities and structured generation via Outlines."""

from __future__ import annotations

from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from eru.common.exceptions import ReasoningError

T = TypeVar("T", bound=BaseModel)
logger = structlog.get_logger(__name__)


class OutlinesGenerator:
    """Base generator that handles structured LLM outputs and schema parsing.

    Attributes:
        model: The underlying Outlines-compatible model or backend pipeline runner.
        max_new_tokens: The upper token threshold cap allocated per target completion.
    """

    def __init__(self, model: Any, max_new_tokens: int = 1024):
        """Initializes the base structural inference client generator."""
        self.model = model
        self.max_new_tokens = max_new_tokens
        logger.info(
            "outlines_generator_base_initialized",
            model_backend_type=type(model).__name__,
            max_new_tokens=max_new_tokens,
        )

    def generate(self, prompt: str, schema: type[T]) -> T:
        """Invokes the model and parses its response into a validated Pydantic schema instance.

        Args:
            prompt: The full structured input prompt text block.
            schema: The target Pydantic model class definition to validate against.

        Returns:
            An instantiated, strongly-typed instance of the requested schema.

        Raises:
            ReasoningError: If validation checks fail or generation crashes.
        """
        schema_name = schema.__name__
        log = logger.bind(
            schema_name=schema_name,
            prompt_length=len(prompt),
        )
        log.info("outlines_structural_generation_invoked")

        try:
            result = self.model(
                prompt,
                schema,
                max_new_tokens=self.max_new_tokens,
            )

            log.debug(
                "outlines_backend_returned_payload",
                payload_type=type(result).__name__,
            )

            if isinstance(result, str):
                return schema.model_validate_json(result)

            if isinstance(result, dict):
                return schema.model_validate(result)

            if isinstance(result, BaseModel):
                return schema.model_validate(result.model_dump())

            return schema.model_validate(result)

        except ValidationError as e:
            log.exception("outlines_output_schema_validation_failed")
            raise ReasoningError(
                f"Structured output violates schema: {e}"
            ) from e
        except Exception as e:
            log.exception("outlines_backend_runtime_crashed")
            raise ReasoningError(f"Generation failed: {e}") from e

    def system_user_prompt(self, system: str, user: str) -> str:
        """Formats standard ChatML text blocks into a single model execution string.

        Args:
            system: Structural context and strict processing guidelines.
            user: Runtime textual data payload configurations.

        Returns:
            A sanitized prompt containing complete chat sequence markup blocks.
        """
        logger.debug(
            "formatting_chatml_prompt",
            system_chars=len(system),
            user_chars=len(user),
        )
        return (
            "<|im_start|>system\n"
            f"{system.strip()}\n"
            "<|im_end|>\n"
            "<|im_start|>user\n"
            f"{user.strip()}\n"
            "<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
