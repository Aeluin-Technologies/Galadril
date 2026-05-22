"""Node/npm wiring."""

load("@npm//:defs.bzl", "npm_link_all_packages")

def define_node_modules():
    """Defines the aggregated node_modules linking target."""
    npm_link_all_packages(name = "node_modules")
