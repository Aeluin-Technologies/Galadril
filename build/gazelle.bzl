"""Gazelle configuration for proto + python."""

load("@gazelle//:def.bzl", "gazelle", "gazelle_binary")

def define_gazelle():
    """Defines Gazelle runner targets."""
    gazelle_binary(
        name = "gazelle_multilang",
        languages = [
            "@gazelle//language/proto",
            "@rules_python_gazelle_plugin//python",
        ],
    )

    gazelle(
        name = "gazelle",
        gazelle = ":gazelle_multilang",
    )
