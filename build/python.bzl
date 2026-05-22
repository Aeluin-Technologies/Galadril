"""Python dependency generation + Gazelle python manifest wiring."""

load("@pip//:requirements.bzl", "all_whl_requirements")
load("@rules_python_gazelle_plugin//manifest:defs.bzl", "gazelle_python_manifest")
load("@rules_python_gazelle_plugin//modules_mapping:def.bzl", "modules_mapping")
load("@rules_uv//uv:pip.bzl", "pip_compile")

def define_python_deps():
    """Defines targets needed for Python deps + Gazelle integration."""
    pip_compile(
        name = "generate_requirements_txt",
        args = [
            "--all-extras",
            "--no-emit-package amarth",
            "--no-emit-package eru",
            "--no-emit-package galadril-inference",
            "--no-emit-package galadril-pipeline",
            "--universal",
            "--generate-hashes",
        ],
        requirements_in = "//:pyproject.toml",
        requirements_txt = "//:requirements.txt",
    )

    modules_mapping(
        name = "modules_map",
        include_stub_packages = True,
        wheels = all_whl_requirements,
    )

    gazelle_python_manifest(
        name = "gazelle_python_manifest",
        modules_mapping = ":modules_map",
        pip_repository_name = "pip",
        requirements = "//:requirements.txt",
    )
