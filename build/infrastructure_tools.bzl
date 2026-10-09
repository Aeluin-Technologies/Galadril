"""Verified upstream tools and charts for the self-contained Ambient test."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive", "http_file")

_TOOLS = {
    "linux_amd64": (
        "linux-amd64",
        "06d8f25bc3a971c4eb29e0ff08429b180402db0f4dec838c9eac427e296800a0",
        "86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb",
        "65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8",
    ),
    "linux_arm64": (
        "linux-arm64",
        "03cde5cf23e6e8e67de5a039ecf26e5b85aca82fba3e5d13dadf904cd218a250",
        "31c5794dd55c66a51e6b7d2e2ac7a114ae8b1de41ff1d9ba51748ac973b06a08",
        "ff749f4b78d9c4f1ec87307df9b50119ed819e2094aa9810cb9acffc3286c8c7",
    ),
    "macos_amd64": (
        "darwin-amd64",
        "b4aabc37534f95b9c764e7823f2df923f50d57600837aa60a06266cce47db732",
        "347a784877e0e20eac865e8d1c36a80f6bb0861d6f29abd34defb6570ef95d92",
        "6851381c486ff6edd691623e3d65c87cb9a5b02887ff8fbbb38d8a031b748387",
    ),
    "macos_arm64": (
        "darwin-arm64",
        "fe106541d5d0a3f18debcd4d432a16f8c0ce3e6ddc06f8fbb6f696a122313e00",
        "d3870437e1e95b67f8edbde964156c84a26503f560821d40c542441658934fba",
        "fd65982c97ddad3106754b69ffa196d0e543aa591930ae52aed1adfb92f8c77f",
    ),
}

_CHARTS = {
    "base": "6a5f3a628e31ea03994f5c8ff44804fcec54ac1b13893f1a3f3c26340c8f2015",
    "istiod": "d49e3ac3100e9c24efbd3a7c26e8e6dd707431f8c06f203182ba54a2e8651a74",
    "cni": "e7cfcbd6842f0b23026dc81bd99edd0e5da87d9f5e2e75b45a2dc2a69f873127",
    "ztunnel": "6fa9f7f88ae68fe7c84df4ef09711639bc8bc38b31769e85336676ebd8ae70b5",
}

def _tools_impl(module_ctx):
    repositories = []
    for platform, (upstream, k3d_sha, helm_sha, kubectl_sha) in _TOOLS.items():
        for tool, url, sha in [
            ("k3d", "https://github.com/k3d-io/k3d/releases/download/v5.9.0/k3d-" + upstream, k3d_sha),
            ("kubectl", "https://dl.k8s.io/release/v1.37.1/bin/" + upstream.replace("-", "/") + "/kubectl", kubectl_sha),
        ]:
            name = tool + "_" + platform
            http_file(name = name, urls = [url], sha256 = sha, executable = True)
            repositories.append(name)
        name = "helm_" + platform
        http_archive(
            name = name,
            urls = ["https://get.helm.sh/helm-v4.3.0-" + upstream + ".tar.gz"],
            sha256 = helm_sha,
            strip_prefix = upstream,
            build_file_content = 'exports_files(["helm"], visibility = ["//visibility:public"])',
        )
        repositories.append(name)
    for chart, sha in _CHARTS.items():
        name = "istio_" + chart
        http_file(
            name = name,
            urls = ["https://istio-release.r2.istio.io/charts/" + chart + "-1.31.1.tgz"],
            downloaded_file_path = chart + ".tgz",
            sha256 = sha,
        )
        repositories.append(name)
    http_file(
        name = "gateway_api",
        urls = ["https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.3/standard-install.yaml"],
        downloaded_file_path = "standard-install.yaml",
        sha256 = "356c2b8e286a964dc20995435659ad1095386329974793af10bbfe3eff977fe5",
    )
    repositories.append("gateway_api")
    return module_ctx.extension_metadata(
        root_module_direct_deps = repositories,
        root_module_direct_dev_deps = [],
        reproducible = True,
    )

infrastructure_tools = module_extension(implementation = _tools_impl)
