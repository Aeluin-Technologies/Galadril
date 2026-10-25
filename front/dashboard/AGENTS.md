# Dashboard rules

Follow the repository and `front/AGENTS.md` instructions as well as this file.

## Current environment

* The application uses Nuxt 4, Vue 3, TypeScript, Pinia, Apollo and Tailwind 3.
  Use the versions resolved by the lockfile, not newer-major documentation.
* Application code lives in `app/`; Nitro code lives in `server/`. Follow Nuxt's
  configured discovery rules and verify generated names after moving components.
  Nested composable directories may require explicit registration.
* `ssr: false` and application-wide KeepAlive are currently configured. Do not
  change rendering strategy as an incidental UI refactor.
* `@galadril/ui/nuxt` registers shared controls with the `Ui` prefix and installs
  the Tailwind preset and token CSS. Do not register a competing UI library.
* `Utils/Button.vue` and `Utils/Modal.vue` delegate to the package for compatibility.
  New code uses `Ui*` directly; migrate remaining specialized overlays separately.
* User-facing strings belong to `i18n/en-US.json`. Reuse Heroicons consistently.

## Responsibilities

* Pages compose features and handle route concerns. Move independent workflows,
  reusable presentation and API implementation out of pages.
* Keep domain components under their feature directory. Split independent
  workflows into named subcomponents with explicit props, emits and slots.
* Treat roughly 250 lines as a review signal, not a mechanical limit. Explain
  retained complexity; do not split cohesive components solely for line count.
* Keep reusable reactive orchestration in composables, API access in services,
  pure transformations in utilities and shared contracts in types.
* Keep temporary UI state local. Use Pinia for genuinely shared state and avoid
  duplicating server data already owned by Apollo's cache.
* Shared UI primitives must not fetch data or resolve business permissions.
  Domain components compose them and own domain-specific behavior.
* Use strict TypeScript for new and changed logic, typed props/emits and separate
  type imports. Do not add `any` or assertions to bypass unresolved contracts.

## Consistency and interaction

* Reuse canonical buttons, fields, dialogs, menus and feedback components.
  Native controls belong inside primitives or documented specialized widgets,
  not copied feature-level implementations of existing controls.
* Express shared visual differences through semantic variants. Do not override
  primitive internals through page-specific CSS or copied utility strings.
* Use `align="start"` for left-aligned button content such as navigation and
  `UiIconButton` for icon-only actions. Avoid conflicting alignment/size classes.
* Use `:action` for automatically tracked async actions, or bind `:loading`
  for externally managed work. Handle `action-error` with contextual feedback.
  Do not simulate a request delay in production controls.
* Every asynchronous workflow needs loading, empty, error and success behavior.
  Keep existing content visible during refresh when it remains valid.
* Prevent duplicate mutations. Preserve input, selection and scroll during
  background updates, and provide explicit success or recoverable error feedback.
* Prefer plain-language tasks and progressive disclosure for administration.
  Preserve the active page during passkey enrollment and session renewal.
* Use NuxtLink for internal navigation and anchors for external destinations or
  downloads. Keep authorization enforced on the server regardless of UI visibility.
* Apply the charter's keyboard, focus, contrast and reduced-motion requirements.
  Test dialog behavior, not just its appearance or ARIA attributes.

## Performance and lifecycle

* Measure production builds before and after performance-sensitive changes.
  Record route, representative data size, environment and relevant metrics.
* Defer heavy graphs, charts, editors, previews and export libraries until needed.
  Do not pull them into the application shell through shared package barrels.
* Use pagination or virtualization when size and measurements justify it.
  Preserve keyboard navigation and access to the complete dataset.
* Prefer computed derived state and stable props. Avoid deep watchers and repeated
  expensive transformations in templates or high-frequency event handlers.
* Cancel stale requests and clean up listeners, timers and subscriptions. Handle
  KeepAlive deactivation as well as unmounting; define reactivation behavior.
* Consider shallow reactivity for large immutable datasets. Optimize measured hot
  paths without sacrificing correctness for speculative allocation savings.
* Reuse the existing OTLP integration for actionable failures and performance
  metrics. Avoid per-render instrumentation, sensitive payloads and unbounded labels.

## Verification and migration

* Before changing behavior, add a failing test of the observable contract.
  Before extraction, protect affected workflows with characterization tests.
* Separate behavior-preserving extraction from intentional UX changes.
* When modifying a shared control, search all consumers and verify affected
  variants and representative pages, including keyboard and asynchronous states.
* Inspect visual changes in a browser at desktop and narrow widths. Use stable
  gallery scenarios for visual regression; snapshots alone do not prove behavior.
* Integrate component tests, accessibility checks, non-mutating lint/format checks
  and complete type checking into Bazel when introducing the shared package.
* `pnpm typecheck` checks the complete application through
  `tsconfig.dashboard.json`; `pnpm typecheck:tools` checks verification scripts
  and tests through `../tsconfig.tools.json`. `pnpm lint` runs non-mutating ESLint
  on utilities, authentication, media, shared code and tests. `pnpm format:check`
  checks formatting without rewriting sources. Do not describe that ESLint scope
  as a complete application-wide Vue lint pass.
* Extend type-check coverage when moving or adding files. Do not let extracted
  components silently fall outside the existing include list.
* Prefer the targeted UI/package and affected dashboard Bazel tests during
  frontend iteration. Run the workspace check when requested by repository policy
  unless the user explicitly requests targeted checks only. Distinguish targeted, whole-workspace
  results, browser evidence and blocked checks in the completion report.
