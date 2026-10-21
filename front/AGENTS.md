# Frontend rules

These rules extend the repository instructions for all files under `front/`.

* Read `front/DESIGN_SYSTEM.md` for visual or interaction changes and
  `front/ui/README.md` before changing shared UI architecture.
* The shared package is `front/ui`, named `@galadril/ui`. It owns visual
  tokens, a Tailwind preset, UI primitives and reusable presentation patterns.
  It must not depend on the dashboard or application-specific business logic.
* Use its declared public exports and Nuxt module. Keep package imports,
  the frontend pnpm lockfile and Bazel source/package targets synchronized.
* Keep one authoritative implementation per shared control. Consolidate existing
  controls incrementally; do not maintain competing local and package versions.
* Dependency direction is application pages/features -> shared patterns -> UI
  primitives -> tokens. Shared UI must not import application stores, API clients,
  authentication policy or feature components.
* Before adding a component, search existing controls and consumers. Reuse an
  existing contract, extend it when appropriate, or explain the distinct need.
* Keep shared component APIs typed and explicit. Prefer props, emits and slots
  over configuration languages, boolean-prop combinations or hidden global state.
* Extract by responsibility, independently testable behavior or actual reuse.
  Avoid trivial wrappers and speculative abstractions.
* Consumer classes may control placement, width and layout. Shared components
  own typography, color, borders, radii, elevation and interaction states.
* Use semantic tokens for application chrome. Raw colors belong only to the
  authoritative token definitions; domain data visualizations may use documented
  palettes with legends and accessible alternatives.
* Treat the charter as intended design, not proof of current compliance.
  Migrate touched workflows in reviewable steps and record remaining consumers.
* For code changes, write failing behavior tests before implementation; establish
  characterization coverage before behavior-preserving extraction. Documentation
  changes require link and consistency review rather than artificial unit tests.
* Expose checks through Bazel. Validate changed shared controls across consumers
  and verify visual changes in a browser. Report failures and missing evidence.
