"""Root package target composition.

This file centralizes root-package (//:...) targets while delegating
implementation to concern-specific files under //build.
"""

load("//:build/platforms.bzl", "define_platforms")
load("//:build/python.bzl", "define_python_deps")
load("//:build/gazelle.bzl", "define_gazelle")
load("//:build/multirun.bzl", "define_multirun")
load("//:build/node.bzl", "define_node_modules")

def define_root_targets():
    """Defines all targets that live in the root package (//)."""
    define_platforms()
    define_python_deps()
    define_gazelle()
    define_multirun()
    define_node_modules()
