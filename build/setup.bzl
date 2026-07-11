"""Root package target composition.

This file centralizes root-package (//:...) targets while delegating
implementation to concern-specific files under //build.
"""

load("//:build/platforms.bzl", "define_platforms")
load("//:build/python.bzl", "define_python_deps")
load("//:build/gazelle.bzl", "define_gazelle")
load("//:build/multirun.bzl", "define_multirun")
# load("//:build/node.bzl", "define_node_modules")

def define_root_targets(name = "platforms"):
    """Defines all targets that live in the root package (//).

    Args:
      name: A unique name for this macro instance.
    """
    native.genrule(
        name = "stamped_tags",
        outs = ["tags.txt"],
        cmd = """
        echo "latest" > $@
        if grep -q "^STABLE_VERSION " bazel-out/stable-status.txt 2>/dev/null; then
            grep "^STABLE_VERSION " bazel-out/stable-status.txt | cut -d' ' -f2 >> $@
        else
            echo "dev" >> $@
        fi
        """,
        stamp = 1,
        visibility = ["//visibility:public"],
    )

    define_platforms(name = name + "_platforms")
    define_python_deps()
    define_gazelle()
    define_multirun()
    # define_node_modules()
