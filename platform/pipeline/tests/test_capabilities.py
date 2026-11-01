"""Regressions for a shared, bounded capability catalog."""

import pytest
from galadril_pipeline.capabilities import (
    Capability,
    CapabilityCatalog,
    builtin_catalog,
)
from pydantic import ValidationError


def descriptor() -> dict[str, object]:
    return {
        "id": "law.algorithm.timeline",
        "plugin_id": "law",
        "plugin_version": "1.0.0",
        "kind": "algorithm",
        "targets": ["pipeline", "scribe"],
        "name": "Timeline",
        "description": "Build a traceable timeline.",
        "parameters_schema": {"type": "object"},
    }


def test_builtin_catalog_covers_native_pipeline_categories() -> None:
    catalog = builtin_catalog()
    assert {item.kind.value for item in catalog.capabilities} >= {
        "input",
        "output",
        "model",
        "algorithm",
    }
    assert all("pipeline" in item.targets for item in catalog.capabilities)
    assert {"core.model.bge-reranker", "core.model.qwen3-reranker"} <= {
        item.id for item in catalog.capabilities
    }
    assert len({item.id for item in catalog.capabilities}) == len(
        catalog.capabilities
    )


def test_native_metadata_and_pipeline_transfer_are_discoverable() -> None:
    catalog = builtin_catalog()
    assert {"core.route.metadata", "core.pipeline.transfer"} <= {
        item.id for item in catalog.capabilities
    }


def test_shared_contract_accepts_a_specialized_official_contribution() -> None:
    capability = Capability.model_validate(descriptor())
    catalog = CapabilityCatalog(capabilities=(capability,))
    assert catalog.capabilities[0].plugin_id == "law"


@pytest.mark.parametrize(
    "update",
    [
        {"id": "other.algorithm.timeline"},
        {"parameters_schema": {"type": "string"}},
        {"targets": []},
        {"targets": ["scribe", "scribe"]},
        {"endpoint": "http://private"},
        {"model_class": "anything"},
        pytest.param({"plugin_id": None}, id="missing-plugin-id"),
        pytest.param({"plugin_version": None}, id="missing-plugin-version"),
        pytest.param(
            {"plugin_id": "user", "id": "user.algorithm.timeline"},
            id="reserved-plugin-namespace",
        ),
    ],
)
def test_rejects_invalid_or_privileged_metadata(
    update: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Capability.model_validate(descriptor() | update)


def test_catalog_rejects_duplicates_and_unknown_versions() -> None:
    item = Capability.model_validate(descriptor())
    with pytest.raises(ValidationError):
        CapabilityCatalog(capabilities=(item, item))
    with pytest.raises(ValidationError):
        CapabilityCatalog(schema_version=2, capabilities=(item,))


def schema_descriptor(serialized_bytes: int) -> dict[str, object]:
    """Sizes the serialized envelope so UTF-8 boundary cases stay exact."""
    schema = {"type": "object", "description": ""}
    item = Capability.model_validate(
        descriptor() | {"parameters_schema": schema}
    )
    overhead = len(
        item.model_dump_json(include={"parameters_schema"}).encode("utf-8")
    )
    pairs, remainder = divmod(serialized_bytes - overhead, 2)
    schema["description"] = "é" * pairs + "a" * remainder
    return descriptor() | {"parameters_schema": schema}


@pytest.mark.parametrize("serialized_bytes", [16383, 16384])
def test_schema_accepts_the_inclusive_utf8_byte_limit(
    serialized_bytes: int,
) -> None:
    item = Capability.model_validate(schema_descriptor(serialized_bytes))

    assert (
        len(item.model_dump_json(include={"parameters_schema"}).encode("utf-8"))
        == serialized_bytes
    )


def test_schema_rejects_the_first_byte_over_its_limit() -> None:
    with pytest.raises(
        ValidationError, match="Capability schema exceeds limit"
    ):
        Capability.model_validate(schema_descriptor(16385))


def catalog_descriptor(serialized_bytes: int) -> dict[str, object]:
    """Spreads padding across valid capabilities to isolate the catalog limit."""
    items = tuple(
        Capability.model_validate(
            descriptor()
            | {
                "id": f"law.algorithm.timeline{index}",
                "parameters_schema": {"type": "object", "description": ""},
            }
        )
        for index in range(16)
    )
    catalog = CapabilityCatalog(capabilities=items)
    padding, remainder = divmod(
        serialized_bytes - len(catalog.model_dump_json().encode("utf-8")),
        len(items),
    )
    padded = tuple(
        Capability.model_validate(
            item.model_dump()
            | {
                "parameters_schema": {
                    "type": "object",
                    "description": "a" * (padding + (index < remainder)),
                }
            }
        )
        for index, item in enumerate(items)
    )
    payload = catalog.model_copy(update={"capabilities": padded})
    assert len(payload.model_dump_json().encode("utf-8")) == serialized_bytes
    return payload.model_dump()


@pytest.mark.parametrize("serialized_bytes", [262143, 262144])
def test_catalog_accepts_the_inclusive_byte_limit(
    serialized_bytes: int,
) -> None:
    catalog = CapabilityCatalog.model_validate(
        catalog_descriptor(serialized_bytes)
    )

    assert len(catalog.model_dump_json().encode("utf-8")) == serialized_bytes


def test_catalog_rejects_the_first_byte_over_its_limit() -> None:
    with pytest.raises(
        ValidationError, match="Capability catalog exceeds limit"
    ):
        CapabilityCatalog.model_validate(catalog_descriptor(262145))


def test_user_skills_preserve_provenance_without_a_plugin() -> None:
    item = descriptor() | {
        "id": "user.skill.research",
        "origin": "user",
        "plugin_id": None,
        "plugin_version": None,
        "kind": "skill",
        "execution": "declared",
        "compatibility": {
            "allowed_models": ["legal"],
            "requires": ["law.mcp.research"],
        },
    }
    skill = Capability.model_validate(item)
    assert skill.plugin_id is None
    assert skill.origin == "user"
    assert skill.compatibility.allowed_models == ("legal",)
    assert (
        Capability.model_validate(item | {"execution": "registered"}).kind
        == "skill"
    )
    for update in [
        {"plugin_id": "law"},
        {"id": "law.skill.research"},
    ]:
        with pytest.raises(ValidationError):
            Capability.model_validate(item | update)


def test_selection_checks_model_dependencies_target_and_execution() -> None:
    dependency = Capability.model_validate(descriptor())
    restricted = Capability.model_validate(
        descriptor()
        | {
            "id": "user.algorithm.analysis",
            "origin": "user",
            "plugin_id": None,
            "plugin_version": None,
            "targets": ["scribe"],
            "compatibility": {
                "allowed_models": ["legal"],
                "requires": [dependency.id],
            },
        }
    )
    declared = Capability.model_validate(
        descriptor() | {"id": "law.algorithm.pending", "execution": "declared"}
    )
    catalog = CapabilityCatalog(capabilities=(dependency, restricted, declared))
    catalog.validate_selection(
        (restricted.id, dependency.id), target="scribe", model="legal"
    )
    for ids, target, model in [
        ((restricted.id, dependency.id), "scribe", "local"),
        ((restricted.id,), "scribe", "legal"),
        ((restricted.id, dependency.id), "pipeline", "legal"),
        ((declared.id,), "scribe", "legal"),
        (("law.algorithm.missing",), "scribe", "legal"),
        ((dependency.id, dependency.id), "scribe", "legal"),
    ]:
        with pytest.raises(ValueError):
            catalog.validate_selection(ids, target=target, model=model)


@pytest.mark.parametrize(
    "constraints",
    [
        {"allowed_models": ["legal", "legal"]},
        {"allowed_models": [""]},
        {"requires": ["law.mcp.research", "law.mcp.research"]},
        {"requires": ["invalid"]},
        {"requires": ["law.algorithm.timeline"]},
        {"allowed_models": [f"model{i}" for i in range(33)]},
    ],
)
def test_rejects_ambiguous_or_unbounded_compatibility(
    constraints: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Capability.model_validate(descriptor() | {"compatibility": constraints})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
