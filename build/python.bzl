"""Gazelle manifest wiring for Python dependencies locked by uv."""

load("@pip//:requirements.bzl", "all_whl_requirements_by_package")
load("@rules_python_gazelle_plugin//manifest:defs.bzl", "gazelle_python_manifest")
load("@rules_python_gazelle_plugin//modules_mapping:def.bzl", "modules_mapping")

def _named_wheel_impl(ctx):
    wheels = ctx.attr.wheel[DefaultInfo].files.to_list()
    if not wheels:
        return [DefaultInfo()]
    if len(wheels) != 1:
        fail("Expected one wheel for {}, got {}".format(ctx.label, len(wheels)))

    output = ctx.actions.declare_file(ctx.attr.filename)
    wheel = wheels[0]
    if wheel.is_directory:
        args = ctx.actions.args()
        args.add_all([wheel], expand_directories = True)
        args.add(output)
        ctx.actions.run_shell(
            arguments = [args],
            command = """
set -eu
test "$#" -eq 2
cp "$1" "$2"
""",
            inputs = [wheel],
            outputs = [output],
        )
    else:
        ctx.actions.symlink(output = output, target_file = wheel)
    return [DefaultInfo(files = depset([output]))]

_named_wheel = rule(
    implementation = _named_wheel_impl,
    attrs = {
        "filename": attr.string(mandatory = True),
        "wheel": attr.label(mandatory = True),
    },
)

def define_python_deps():
    """Defines the Gazelle manifest generated from uv-locked wheels."""
    gazelle_wheels = {}
    for package, wheel in all_whl_requirements_by_package.items():
        # Licorne is loaded dynamically and its VCS source is not a wheel.
        if package == "licorne":
            continue

        target = "_gazelle_wheel_" + package
        _named_wheel(
            name = target,
            filename = "{}-0-py3-none-any.whl".format(package),
            wheel = wheel,
        )
        gazelle_wheels[package] = ":" + target

    modules_mapping(
        # Linux wheels expose bundled shared libraries as false Python modules.
        exclude_patterns = [
            "^_|(\\._)+",
            "^cuda\\.bindings$",
            "^lib\\.",
            "^nvidia\\..*\\.lib\\.",
            "^opencv_python(_headless)?\\.libs\\.",
            "^triton$",
        ],
        name = "gazelle_python_modules",
        wheels = select({
            # Aspect currently ORs NVIDIA's Linux marker with a looser marker.
            "@platforms//os:macos": [
                wheel
                for package, wheel in gazelle_wheels.items()
                if not package.startswith("nvidia_")
            ],
            "//conditions:default": gazelle_wheels.values(),
        }),
    )

    gazelle_python_manifest(
        name = "gazelle_python_manifest",
        modules_mapping = ":gazelle_python_modules",
        pip_repository_name = "pip",
        requirements = ["//:uv.lock"],
    )
