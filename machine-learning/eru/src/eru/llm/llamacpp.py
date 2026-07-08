"""llama.cpp GGUF backend for schema-constrained Eru generation."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from os import PathLike
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eru.common.exceptions import ModelResolutionError, ReasoningError

logger = structlog.get_logger(__name__)


class LlamaCppConfig(BaseModel):
    """Runtime configuration for a caller-managed GGUF artifact."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        protected_namespaces=(),
    )

    model_path: str | PathLike[str] | None = None
    n_ctx: int = 4096
    n_threads: int | None = None
    n_gpu_layers: int = -1
    seed: int = 0
    verbose: bool = False
    chat_format: str | None = None
    temperature: float = 0.0
    top_p: float = 1.0
    repeat_penalty: float = 1.05
    stop: tuple[str, ...] = Field(default=("</s>", "<|im_end|>"))


@dataclass(slots=True)
class LlamaCppJsonModel:
    """Callable JSON model adapter shared by all Eru LLM pipeline stages."""

    llama: Any
    temperature: float = 0.0
    top_p: float = 1.0
    repeat_penalty: float = 1.05
    stop: tuple[str, ...] = ("</s>", "<|im_end|>")
    _lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Creates a guard because llama.cpp contexts are not safely reentrant."""
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: LlamaCppConfig) -> LlamaCppJsonModel:
        """Loads llama.cpp from an explicit path without downloading artifacts."""
        if config.model_path is None:
            logger.error("gguf_load_failed_missing_path")
            raise ModelResolutionError(
                "A GGUF model_path is required to load llama.cpp."
            )

        try:
            from llama_cpp import Llama
        except ImportError as exc:
            logger.exception("llama_cpp_import_missing")
            raise ModelResolutionError(
                "llama-cpp-python is required to load GGUF models."
            ) from exc

        kwargs: dict[str, Any] = {
            "model_path": str(config.model_path),
            "n_ctx": config.n_ctx,
            "n_gpu_layers": config.n_gpu_layers,
            "seed": config.seed,
            "verbose": config.verbose,
        }
        if config.n_threads is not None:
            kwargs["n_threads"] = config.n_threads
        if config.chat_format is not None:
            kwargs["chat_format"] = config.chat_format

        logger.info(
            "loading_gguf_model_instance",
            model_path=kwargs["model_path"],
            n_ctx=config.n_ctx,
            n_gpu_layers=config.n_gpu_layers,
        )

        try:
            llama = Llama(**kwargs)
        except Exception as exc:
            logger.exception(
                "gguf_instantiation_failed", model_path=str(config.model_path)
            )
            raise ModelResolutionError(
                f"Failed to load GGUF model from '{config.model_path}': {exc}"
            ) from exc

        logger.info(
            "gguf_model_loaded_successfully", model_path=str(config.model_path)
        )
        return cls(
            llama=llama,
            temperature=config.temperature,
            top_p=config.top_p,
            repeat_penalty=config.repeat_penalty,
            stop=config.stop,
        )

    def __call__(
        self,
        prompt: str,
        schema: type[BaseModel],
        max_new_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Generates JSON and validates it before returning a plain payload."""
        schema_name = schema.__name__
        response_schema = schema.model_json_schema()
        schema_prompt = (
            f"{prompt}\n\n"
            "Return only a JSON object matching this JSON Schema:\n"
            f"{json.dumps(response_schema, ensure_ascii=False)}\n"
        )

        log = logger.bind(
            schema_name=schema_name,
            max_new_tokens=max_new_tokens,
            prompt_length=len(prompt),
        )
        log.info("llama_cpp_generation_started")

        with self._lock:
            log.debug("acquired_reentrant_context_lock")
            raw = self.llama(
                schema_prompt,
                max_tokens=max_new_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
                repeat_penalty=self.repeat_penalty,
                stop=list(self.stop),
            )

        log.debug("released_reentrant_context_lock")

        text = self._extract_text(raw)
        payload = self._parse_json_object(text)

        try:
            validated_dump = schema.model_validate(payload).model_dump()
            log.info("llama_cpp_generation_completed_and_validated")
            return validated_dump
        except ValidationError as exc:
            log.exception("llama_cpp_response_schema_invalid")
            raise ReasoningError(
                f"llama.cpp response violates requested schema: {exc}"
            ) from exc

    def _extract_text(self, raw: Any) -> str:
        """Extracts completion text from llama-cpp-python response variants."""
        if isinstance(raw, str):
            return raw
        if not isinstance(raw, dict):
            logger.error(
                "unexpected_llama_cpp_response_type",
                received_type=type(raw).__name__,
            )
            raise ReasoningError(
                f"Unexpected llama.cpp response type: {type(raw)!r}"
            )

        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            logger.error("llama_cpp_returned_empty_choices")
            raise ReasoningError("llama.cpp returned no completion choices.")

        first = choices[0]
        if not isinstance(first, dict):
            logger.error("llama_cpp_invalid_choice_payload_shape")
            raise ReasoningError(
                "llama.cpp returned an invalid choice payload."
            )

        text = first.get("text")
        if isinstance(text, str):
            return text

        message = first.get("message")
        if isinstance(message, dict) and isinstance(
            message.get("content"), str
        ):
            return message["content"]

        logger.error("llama_cpp_missing_text_content_fields")
        raise ReasoningError("llama.cpp completion did not contain text.")

    def _parse_json_object(self, text: str) -> dict[str, Any]:
        """Parses a JSON object, tolerating model wrappers around the payload."""
        stripped = text.strip()

        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines[0].startswith("```"):
                lines.pop(0)
            if lines and lines[-1].startswith("```"):
                lines.pop()
            stripped = "\n".join(lines).strip()

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.error("json_parsing_failed", error=str(exc))
            raise ReasoningError(
                f"llama.cpp response contained invalid JSON: {exc}"
            ) from exc

        if isinstance(payload, list):
            logger.warning(
                "json_payload_is_list_encapsulating_in_relations_dict"
            )
            return {"relations": payload}

        if not isinstance(payload, dict):
            logger.error(
                "parsed_json_payload_is_not_an_object",
                received_type=type(payload).__name__,
            )
            raise ReasoningError("llama.cpp response JSON must be an object.")

        return payload
