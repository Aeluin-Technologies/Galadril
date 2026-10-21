# @galadril/ui

Private Vue 3 component package for Galadril. It implements the
[visual charter](../DESIGN_SYSTEM.md) with semantic CSS tokens, a Tailwind 3 preset
and a Nuxt 4 module. It has no application stores, API clients or authorization
policy. Vue and Heroicons are peer dependencies; the host owns their instances.

## Nuxt integration

The frontend pnpm workspace and lockfile live in `front/`. The dashboard declares
`"@galadril/ui": "workspace:*"` and registers this module:

```ts
export default defineNuxtConfig({
  modules: ["@galadril/ui/nuxt"],
});
```

The module installs `@nuxtjs/tailwindcss`, adds the preset and token stylesheet,
registers `Ui*` components on demand, and includes the package's Vue/TypeScript
sources in Tailwind's content scan. Keep application-specific Tailwind extensions
in the application's config; do not repeat shared token values there.

## Public entry points

| Export                    | Use                                                 |
| ------------------------- | --------------------------------------------------- |
| `@galadril/ui/nuxt`       | Nuxt module                                         |
| `@galadril/ui/tailwind`   | Tailwind 3 preset                                   |
| `@galadril/ui/tokens.css` | Light-theme semantic custom properties              |
| `@galadril/ui/components` | Named Vue component exports                         |
| `@galadril/ui/types`      | Action, variant, size, tone and select-option types |

For a Vue application without Nuxt, import the token stylesheet once, add the
preset to Tailwind, include the package's resolved `src/**/*.{vue,ts}` files in
its content configuration, and import components explicitly. The preset alone
cannot make Tailwind discover component classes. It supports opacity modifiers
such as `bg-primary/10` and does not install a second preflight.

## Components

| Component      | Contract                                                                                                                                            |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `UiButton`     | `primary`, `secondary`, `ghost`, `danger`; `sm`, `md`, `lg`; `align="start"` or `center`; leading/trailing slots                                    |
| `UiIconButton` | Required accessible `label` and Heroicons `icon`; tooltip defaults to the label                                                                     |
| `UiTooltip`    | Element `target` and `text`; responsive text, trigger-anchored arrow, focus/hover, Escape and native popover top layer                              |
| `UiSpinner`    | Decorative Heroicons loading animation with reduced-motion support                                                                                  |
| `UiDialog`     | `v-model:open`, title, description, close label, optional dismissal; default/footer slots                                                           |
| `UiField`      | Label, help, error and required indicator; slot receives `id`, `describedby`, `invalid`                                                             |
| `UiInput`      | String `v-model`, label, help/error, native input attributes                                                                                        |
| `UiSelect`     | String `v-model`, typed options with nonempty values, label/help/error, placeholder/name; styled Reka UI listbox, keyboard navigation and typeahead |
| `UiCheckbox`   | Boolean `v-model`, label, optional help and disabled state                                                                                          |
| `UiBadge`      | Text and icon for neutral/info/success/warning/danger tones                                                                                         |
| `UiAlert`      | Accessible result feedback; danger is an alert, other tones are a status                                                                            |
| `UiCard`       | Optional title/description, content, actions and footer                                                                                             |
| `UiPageHeader` | Page title, eyebrow, description and actions                                                                                                        |
| `UiEmptyState` | Empty-state title, explanation and action slot                                                                                                      |

UI primitives own color, typography, radii, elevation and interaction states.
Use consumer classes for layout. Use `align="start"` for navigation instead of
adding a contradictory `justify-start` to a centered button. Use `UiIconButton`
for a consistently sized square target and a required accessible label.

## Async feedback

Pass the real operation through `action` for automatic pending state:

```vue
<UiButton
  :action="save"
  :pending-label="$t('actions.working')"
  @action-error="showSaveError"
>
  {{ $t('actions.save') }}
</UiButton>
```

An action receives the click event and returns void or a promise. While pending,
repeat activation is blocked, an animated Heroicons spinner appears, `aria-busy`
and `aria-disabled` are set, and a separate polite status announces `pendingLabel`.
The focused button remains focusable. The original label remains visible unless
`loadingLabel` is supplied. State resets after success or failure. Handle
`action-error` with a contextual message; raw exceptions never go into UI telemetry.

For state owned by a form or store, bind `loading` explicitly. A normal Vue `click`
listener does not automatically expose its returned promise to the button. There
are no artificial delays in production components; the playground simulates a
request solely to demonstrate visible pending and result states.

Action outcomes and duration use `@opentelemetry/api` counters/histograms with
bounded labels. The host configures its OTLP provider; the package installs no
exporter, captures no payloads and emits no per-render telemetry.

## Accessibility

Tooltips open on keyboard focus and pointer hover, remain available while hovered,
and dismiss on Escape. An open tooltip consumes Escape before a containing dialog.
They use `aria-describedby` and preserve existing descriptions. Important help
belongs in visible field text; tooltips do not replace labels or explain errors.
Use localized labels and messages from the consuming application. `tooltip=false`
explicitly disables a tooltip.

Floating UI keeps the arrow aligned with the trigger center during scrolling,
resizing and layout changes. Tooltips prefer the top edge and flip below when
there is insufficient room. Text scales from 12 to 14 px, wraps within the
viewport and leaves an 8 px margin. Position observers run only while open.

Select popups share the semantic palette, show a check beside the selected
option and support arrows, Home/End, typeahead and Escape with focus restoration.
Reka UI supplies the headless keyboard and focus behavior; the package owns the
visual styling. A portal stays inside a containing native dialog so it remains
interactive in the modal top layer. The hidden native control preserves named
form values and required/disabled semantics.

Dialogs use native `showModal()` for an inert background and modal semantics,
contain the Tab cycle and restore the opener after closing. Use native `autofocus`
on the intended initial field. `dismissible=false` requires an explicit accessible
completion/exit action from the consumer. Inputs and selects associate labels,
validation and help; status colors are paired with words and icons.

Animations respect reduced motion. Automated accessibility checks complement
keyboard and visual checks; they do not certify screen-reader compatibility.

## Development and verification

From the repository root:

```sh
pnpm --dir front install --frozen-lockfile
pnpm --dir front/ui dev
```

The standalone example runs at `http://127.0.0.1:3101`. It contains local fixture
data and a legacy-adapter route; it does not bypass dashboard authentication or
connect to production services. `playground/app/components/DataSources.vue` and
`WorkspaceView.vue` separate source filtering from workspace orchestration.

Install Playwright Chromium for the execution host, then pass its cache explicitly
because Bazel gives tests an isolated HOME:

```sh
pnpm --dir front/ui exec playwright install chromium
bazel test //front/ui:browser_test --test_env=PLAYWRIGHT_BROWSERS_PATH=/path/to/browser-cache
bazel test //front/ui:typecheck_test //front/ui:lint_test //front/ui:format_test
bazel test //front/ui:production_test //front/ui:staging_test //build/private:nuxt_staging_test
```

CI provisions the same browser through `//front/ui:install_browser -- --with-deps`
and passes its cache to both test and coverage jobs. No browser is downloaded by
the test itself.

The browser test reserves an available loopback port and starts its own Nuxt
fixture. `GALADRIL_UI_TEST_ORIGIN` optionally selects an already-running local
fixture for interactive development. Tests cover real browser behavior, disabled
and duplicate activation, failure recovery, legacy adapters, focus, tooltips,
validation, styled selection, modal portals, alignment, narrow layouts, reduced motion and axe
WCAG checks. The production test compiles a Nuxt consumer, checks its types and
verifies emitted semantic CSS. The OCI staging tests check materialized sibling
sources and the dashboard build directory; they do not prove a published image.

## Dashboard migration

`Utils/Button.vue` and `Utils/Modal.vue` are temporary delegating adapters. They
preserve existing caller events, attributes and slots without separate styling.
New consumers use `Ui*` directly. Existing feature-specific dialogs, menus and
page-specific styles remain incremental migration work; do not duplicate them
in this package without establishing a reusable, tested contract.

The Bazel `pkg` target exposes the workspace package. Dashboard OCI builds stage
`front/` as their source root, install the frontend workspace and build from
`dashboard/`. Keep source targets, manifests and the shared lockfile synchronized.

## Static outage page

`@galadril/ui/static/service-unavailable.html` is the English, script-free proxy
fallback. Its tokens and branding are embedded at build time so it does not need
Vue, Nuxt, fonts, or image upstreams during an outage. Regenerate its committed
Envoy and Kubernetes filters after changing the template, tokens, logo, or Lua:

```sh
node infrastructure/envoy/build-error-page.mjs
```

The proxy keeps HTTP status codes and API responses unchanged. Browser HTML
navigations receive escaped diagnostics and a request reference matching
`x-request-id` and the OTLP `request.id` field. An actual trace ID is also shown
when Envoy exposes one; an absent span is never replaced by a fabricated ID.
The generated copies are checked by `//infrastructure/envoy/tests:error_page_test`.
