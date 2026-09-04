from __future__ import annotations

import importlib
from typing import Protocol, cast


class _ModelFactory(Protocol):
    """Constructor contract for dynamically configured inference models."""

    def __call__(self, **kwargs: object) -> object: ...


def import_string(path: str) -> object:
    """Dynamically import a class/module from a string path.

    Args:
        path: The full dot-separated path to the class or module.

    Returns:
        The imported class or module attribute.
    """
    module_name, class_name = path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def build_model(
    model_path: str,
    *,
    artifact_path: str | None = None,
    **kwargs: object,
) -> object:
    """Instantiate a model dynamically (useful for local sync execution).

    Args:
        model_path: The full dot-separated path to the model class.
        artifact_path: Optional filesystem path to resolved model artifacts.
        **kwargs: Configuration arguments passed directly to the model constructor.

    Returns:
        An instance of the dynamically loaded model class.
    """
    model_cls = import_string(model_path)
    if not callable(model_cls):
        raise TypeError(f"Configured model is not callable: {model_path}")
    factory = cast(_ModelFactory, model_cls)
    if artifact_path is not None:
        kwargs.setdefault("artifact_path", artifact_path)
    return factory(**kwargs)
