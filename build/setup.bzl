load("@rules_shell//shell:sh_test.bzl", "sh_test")
load(":gazelle.bzl", "define_gazelle")
load(":multirun.bzl", "define_multirun")
load(":platforms.bzl", "define_platforms")
load(":python.bzl", "define_python_deps")

def define_root_targets(name = "platforms"):
    native.genrule(
        name = "image_version_tag",
        srcs = ["//:VERSION.txt"],
        tools = ["//build:version_to_tag"],
        outs = ["image_version_tag.txt"],
        cmd = "$(location //build:version_to_tag) $(location //:VERSION.txt) $@",
        visibility = ["//visibility:public"],
    )

    native.genrule(
        name = "stamped_tags",
        srcs = ["//:VERSION.txt"],
        tools = ["//build:version_to_tags"],
        outs = ["tags.txt"],
        cmd = "$(location //build:version_to_tags) $(location //:VERSION.txt) $@",
        visibility = ["//visibility:public"],
    )

    sh_test(
        name = "version_test",
        srcs = ["//build:version_test.sh"],
        data = ["//:VERSION.txt"],
        visibility = ["//visibility:public"],
    )

    define_platforms(
        name = name + "_platforms",
    )

    define_python_deps()
    define_gazelle()
    define_multirun()
