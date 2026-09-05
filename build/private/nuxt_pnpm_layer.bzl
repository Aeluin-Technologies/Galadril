def _relative_path(file, package):
    prefix = package + "/"
    path = file.short_path

    if not path.startswith(prefix):
        fail(
            "Source %s is outside package //%s" % (
                path,
                package,
            ),
        )

    return path[len(prefix):]

def _nuxt_pnpm_layer_impl(ctx):
    output = ctx.actions.declare_file(
        ctx.label.name + ".tar",
    )

    manifest = ctx.actions.declare_file(
        ctx.label.name + ".sources.tsv",
    )

    manifest_content = "\n".join([
        "%s\t%s" % (
            file.path,
            _relative_path(
                file,
                ctx.label.package,
            ),
        )
        for file in ctx.files.srcs
    ]) + "\n"

    ctx.actions.write(
        output = manifest,
        content = manifest_content,
    )

    command = """
set -euo pipefail

manifest="$1"
output="$2"
builder_image="$3"
container_platform="$4"
pnpm_version="$5"
build_script="$6"
package_dir="$7"

case "${package_dir}" in
    ""|/*|*".."*)
        echo "Invalid package directory: ${package_dir}" >&2
        exit 1
        ;;
esac

execution_root="$(pwd -P)"
output_absolute="${execution_root}/${output}"
temporary_directory="$(mktemp -d "${execution_root}/nuxt-pnpm-layer.XXXXXXXX")"

cleanup() {
    rm -rf "${temporary_directory}"
}

trap cleanup EXIT

project_directory="${temporary_directory}/project"

mkdir -p "${project_directory}"

while IFS="$(printf '\\t')" read -r source relative; do
    if [ -z "${source}" ]; then
        continue
    fi

    destination="${project_directory}/${relative}"

    mkdir -p "$(dirname "${destination}")"
    cp -a "${source}" "${destination}"
done < "${manifest}"

temporary_absolute="$(cd "${temporary_directory}" && pwd -P)"

docker run \
    --rm \
    --platform "${container_platform}" \
    --volume "${temporary_absolute}:/workspace" \
    --workdir /workspace/project \
    --env "PNPM_VERSION=${pnpm_version}" \
    --env "BUILD_SCRIPT=${build_script}" \
    --env "PACKAGE_DIR=${package_dir}" \
    --env NITRO_PRESET=node-server \
    --env NODE_ENV=production \
    "${builder_image}" \
    /bin/bash \
    -ceu '
        set -o pipefail

        apt-get update

        apt-get install \
            --yes \
            --no-install-recommends \
            ca-certificates \
            libatomic1

        rm -rf /var/lib/apt/lists/*

        npm install --global corepack

        export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
        corepack enable
        corepack prepare "pnpm@${PNPM_VERSION}" --activate

        pnpm --version

        pnpm install \
            --frozen-lockfile

        pnpm run \
            "${BUILD_SCRIPT}"

        test -f .output/server/index.mjs

        mkdir -p "/workspace/rootfs/${PACKAGE_DIR}"

        cp -a \
            .output \
            "/workspace/rootfs/${PACKAGE_DIR}/.output"

        tar \
            --sort=name \
            --mtime=@0 \
            --owner=0 \
            --group=0 \
            --numeric-owner \
            --format=gnu \
            -cf /workspace/layer.tar \
            -C /workspace/rootfs \
            .

        test -s /workspace/layer.tar

        chmod -R a+rwX /workspace
    '

cp \
    "${temporary_directory}/layer.tar" \
    "${output_absolute}"
"""

    ctx.actions.run_shell(
        inputs = ctx.files.srcs + [
            manifest,
        ],
        outputs = [
            output,
        ],
        arguments = [
            manifest.path,
            output.path,
            ctx.attr.builder_image,
            ctx.attr.container_platform,
            ctx.attr.pnpm_version,
            ctx.attr.build_script,
            ctx.attr.package_dir,
        ],
        command = command,
        execution_requirements = {
            "requires-network": "1",
        },
        mnemonic = "NuxtPnpmBuild",
        progress_message = "Building Nuxt layer %{label}",
        use_default_shell_env = True,
    )

    return [
        DefaultInfo(
            files = depset([
                output,
            ]),
        ),
    ]

nuxt_pnpm_layer = rule(
    implementation = _nuxt_pnpm_layer_impl,
    attrs = {
        "srcs": attr.label_list(
            allow_files = True,
            mandatory = True,
        ),
        "builder_image": attr.string(
            mandatory = True,
        ),
        "container_platform": attr.string(
            mandatory = True,
        ),
        "pnpm_version": attr.string(
            mandatory = True,
        ),
        "build_script": attr.string(
            default = "build",
        ),
        "package_dir": attr.string(
            default = "src",
        ),
    },
)
