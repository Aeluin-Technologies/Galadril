# Galadril visual and interaction charter

Status: normative design direction, implemented by `@galadril/ui` primitives and
the example workspace. Existing dashboard screens migrate incrementally.

## Product character

Build a luminous operational workspace inspired by the clarity and information
hierarchy of analytical consoles: white material surfaces, structured panels,
precise alignment and restrained elevation. Amber identifies primary actions and
selection. Rounded corners soften the interface without turning every element
into a pill. The product is light-only; do not add dark variants or a theme switch.

Prioritize understanding and task completion. Organize screens around user goals,
reveal advanced settings progressively and keep related actions near their data.
Use explicit names instead of exposing infrastructure terminology by default.

Authentication keeps one visible task per step: a compact white panel on the
neutral canvas, a clear title, the required fields and one primary action.
Keep the organization sign-in explanation and the notice that credentials and
access are managed by the user's organization; this context matters in a
multi-tenant product.
Preserve visible panel boundaries and balanced spacing; removing secondary
content must not leave an unstructured form floating in an empty page. Avoid
promotional sections, feature lists and repeated access headings.

## Semantic color tokens

These are the initial approved values. Their executable source of truth is
the UI package's token stylesheet. Applications consume semantic Tailwind names;
they must not copy these hex values into individual components.

| Token                | Value     | Intended use                            |
| -------------------- | --------- | --------------------------------------- |
| `canvas`             | `#F8FAFC` | Application background                  |
| `surface`            | `#FFFFFF` | Panels, menus, dialogs                  |
| `surface-muted`      | `#F1F5F9` | Headers and secondary grouping          |
| `surface-hover`      | `#E2E8F0` | Neutral hover background                |
| `border`             | `#E2E8F0` | Decorative panel separators             |
| `border-control`     | `#64748B` | Necessary control boundaries on white   |
| `foreground`         | `#17212F` | Primary text and amber button labels    |
| `muted`              | `#526173` | Supporting text and placeholders        |
| `primary`            | `#F59E0B` | Primary action background               |
| `primary-hover`      | `#D97706` | Primary action hover background         |
| `primary-active`     | `#B45309` | Primary action pressed background       |
| `on-primary`         | `#17212F` | Labels on primary and hover backgrounds |
| `on-primary-active`  | `#FFFFFF` | Labels on the darker pressed background |
| `primary-subtle`     | `#FFFBEB` | Selected or emphasized background       |
| `primary-foreground` | `#92400E` | Amber text on light backgrounds         |
| `focus`              | `#92400E` | Visible keyboard focus                  |
| `info`               | `#1D4ED8` | Links and informational emphasis        |
| `info-subtle`        | `#EFF6FF` | Informational background                |
| `success`            | `#047857` | Success labels and icons                |
| `success-subtle`     | `#ECFDF5` | Success background                      |
| `warning`            | `#92400E` | Warning labels and icons                |
| `warning-subtle`     | `#FFFBEB` | Warning background                      |
| `danger`             | `#B91C1C` | Error labels and destructive actions    |
| `danger-subtle`      | `#FEF2F2` | Error background                        |

White text on `primary` has approximately 2.15:1 contrast; white on
`primary-hover` has approximately 3.19:1. Neither is suitable for normal-sized
text. `foreground` on `primary` has approximately 7.55:1. Use the approved
foreground/background pairs and test every additional state, including opacity
and overlays. A decorative separator is not a sufficient control boundary.

Keep neutral surfaces dominant. Blue provides a cool supporting accent; it must
not compete with amber for primary actions. Warning and selection may share an
amber family but need distinct text, icons and placement. Charts require their
own documented categorical/sequential palettes and non-color interpretation.

## Typography, spacing and material

- Use the application's system sans-serif stack. Use monospace for code and
  technical identifiers, and tabular numerals for comparable numeric columns.
- Default UI text: 14 px with 20 px line height. Supporting metadata: 12/16 px.
  Body reading text: 16/24 px. Section titles: 18/24 px; page titles: 24/32 px.
- Use a 4 px spacing base: 4, 8, 12, 16, 24, 32 and 48 px. Prefer 16–24 px
  panel padding and 8–12 px between related controls.
- Radius tokens: control 8 px, panel 12 px, dialog 16 px. Pills are reserved for
  status badges and suitable chips.
- Elevation tokens: panel `0 1px 2px rgb(23 33 47 / 0.06)`, popover
  `0 4px 12px rgb(23 33 47 / 0.10)`, dialog
  `0 16px 48px rgb(23 33 47 / 0.16)`. Use borders for routine separation.
- Control heights: compact 32 px, default 36 px, comfortable 44 px. Small visible
  icons still need adequate interactive targets and accessible names.
- Icons: Heroicons, normally 16 or 20 px. Keep stroke style consistent within
  a control group. Decorative icons are hidden from assistive technology.
- Transition tokens: fast 120 ms, default 180 ms. Prefer opacity and transform;
  respect reduced motion and avoid layout animation on data-intensive screens.
- Layer tokens: base 0, sticky 10, dropdown 20, modal-backdrop 30, modal 40,
  toast 50. Overlays inside dialogs need a dialog-aware portal/layer strategy;
  do not fix stacking problems with arbitrary z-index values.

## Component and page contracts

Navigation aligns icon and label columns to the start edge. Center short action
labels within their buttons, not whole navigation entries. Selects use a decorative
Heroicons chevron with consistent end padding and vertical centering. Inputs,
selects and default buttons share a 36 px control height.

Buttons have primary, secondary, ghost and danger variants. Their size, loading,
disabled, focus, hover and pressed states belong to the shared implementation.
Use at most one dominant primary action per task region. Destructive actions use
explicit verbs, with confirmation or undo according to their impact.

Fields have persistent labels, optional help and associated validation messages.
Placeholders never replace labels. Use consistent required/optional conventions.
Prefer inline validation without deleting or resetting valid input.

Pages share a header pattern with title, short context and actions. Dense
workspaces may use split panes and toolbars. Tables use aligned headers, stable
row identity, clear selection, sort/filter feedback and discoverable pagination.
Avoid repeated nested cards when spacing and a heading express the hierarchy.

Every remote-data view defines initial loading, background refresh, empty,
filtered-empty, error, success and permission-denied behavior where applicable.
Skeletons reserve space; refresh keeps valid content visible. Empty states explain
the next action. Errors offer a safe retry without leaking internal exceptions.
Announce important results accessibly without repeatedly interrupting users.

Tooltips point to the horizontal center of their trigger, preferably above it.
Keep the bubble inside the viewport, moving its arrow independently near edges;
flip below only when the top lacks space. Scale text and padding responsively,
retain at least 12 px text, and wrap long labels. Tooltips must not intercept
activation of their trigger or inherit spacing from the surrounding page.

Select menus use the shared white surface, panel radius, subtle elevation,
amber highlight and a selected-item check. Preserve labels, keyboard navigation,
typeahead, disabled options and focus restoration. Escape closes only the active
menu before its containing dialog; portals inside modals remain interactive.

Dialogs own accessible naming, initial focus, focus containment, inert background,
Escape dismissal and focus restoration. Unsaved changes and mandatory enrollment
need an explicit, accessible exit policy. Drawers are for contextual detail;
modals are for focused decisions. Avoid stacking dialogs when an in-place step
or replacement panel would be clearer.

## Accessibility and responsive behavior

Target WCAG 2.2 AA. Normal text needs at least 4.5:1 contrast; qualifying large
text needs 3:1. Necessary control boundaries and visual state indicators need
3:1 against adjacent colors. Check actual rendered combinations.

All actions must work with a keyboard and visible focus. Use native semantics,
accessible names and correct state attributes. Never communicate status through
color alone. Aim for 44 px touch targets; at minimum satisfy WCAG 2.2 target-size
requirements and applicable spacing rules. Verify zoom and content reflow.

At narrow widths, preserve the main task, move secondary tools into accessible
drawers and stack appropriate forms. Wide analytical tables may scroll within
a clearly identified region; the entire page must not accidentally overflow.

## Evidence and change control

Maintain an internal gallery of approved components and states when implementing
the package. Include long text, keyboard focus, validation, loading and narrow
layouts. Shared style changes require review of affected consumers and gallery
states, plus stable visual regression coverage where available.

Establish production performance baselines for representative routes and datasets:
initial transferred JS/CSS, route chunk size, main-thread work, interaction latency,
request counts and retained memory across repeated navigation. Separate initial
load, route transitions and sustained use. Set CI budgets from these baselines;
do not claim improvement from smaller source files alone.

References: [WCAG contrast](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html),
[non-text contrast](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html),
[target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html),
[dialog behavior](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/).
