"""Defines the per-scenario runfiles and Docker isolation contract."""

load("@rules_python//python:defs.bzl", "py_test")
load("//build:testing.bzl", "DOCKER_E2E_TEST_EXEC_PROPERTIES", "DOCKER_TEST_EXEC_PROPERTIES")

def pipeline_e2e_test(name, src, deps, data = [], intake = False, scribe = False, vision = False, tags = []):
    """Declares only the images the scenario can start."""
    images = ["gateway", "registry", "studio"]
    if intake:
        images.append("intake")
    if vision:
        images.append("vision")
    if scribe:
        images += ["scribe", "mcp", "chat_model"]

    streaming = intake or vision
    streaming_data = [
        "//schemas:avro_schema",
        "//tests/e2e/fixtures:e2e_inference_model.py",
    ] if streaming else []

    # Compose still pulls infrastructure and browser dependencies at execution.
    # Cache the OCI artifacts while re-running the live test in a clean daemon.
    py_test(
        name = name,
        size = "enormous" if streaming else "large",
        timeout = "eternal",
        srcs = [src],
        main = src,
        data = [":compose_runtime_configuration"] + [":load_" + image for image in images] + streaming_data + data,
        deps = [":e2e_environment", "@pip//pytest"] + deps,
        env = {"GALADRIL_E2E_IMAGES": " ".join(images)},
        exec_properties = DOCKER_E2E_TEST_EXEC_PROPERTIES if streaming else DOCKER_TEST_EXEC_PROPERTIES,
        legacy_create_init = False,
        tags = [
            "exclusive-if-local",
            "integration",
            "no-cache",
            "no-sandbox",
            "requires-docker",
        ] + tags,
    )
