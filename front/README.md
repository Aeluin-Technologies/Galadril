# Galadril frontend

The dashboard is a Nuxt application in `dashboard/`. Bazel provides its tests
and OCI image targets; `front/BUILD.bazel` exposes dashboard image aliases.

Read [AGENTS.md](AGENTS.md) before changing frontend code and
[dashboard/AGENTS.md](dashboard/AGENTS.md) for dashboard-specific conventions.
The [visual charter](DESIGN_SYSTEM.md) defines the shared visual and interaction
contract. The [internal UI package](ui/README.md) documents its API
boundary and migration sequence.

`@galadril/ui` provides shared Vue components, semantic tokens, a Tailwind 3 preset
and a Nuxt module. The pnpm workspace and lockfile live here in `front/`.
The dashboard's existing button and modal adapters delegate to the package.

Install with `pnpm --dir front install --frozen-lockfile` from the repository root.
Run `pnpm --dir front/ui dev` for the standalone example at `http://127.0.0.1:3101`.

For interactive dashboard development, use `pnpm dev` from `dashboard/`.
Use Bazel for automated verification, including `bazel test //...` from the
repository root. Follow the dashboard instructions for validation limitations.
