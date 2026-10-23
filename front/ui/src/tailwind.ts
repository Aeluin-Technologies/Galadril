import type { Config } from "tailwindcss";

const names = [
  "canvas",
  "surface",
  "surface-muted",
  "surface-hover",
  "border",
  "border-control",
  "foreground",
  "muted",
  "primary",
  "primary-hover",
  "primary-active",
  "on-primary",
  "on-primary-active",
  "primary-subtle",
  "primary-foreground",
  "focus",
  "info",
  "info-subtle",
  "success",
  "success-subtle",
  "warning",
  "warning-subtle",
  "danger",
  "danger-subtle",
] as const;

export default {
  theme: {
    extend: {
      colors: Object.fromEntries(
        names.map((name) => [name, `rgb(var(--ui-${name}) / <alpha-value>)`]),
      ),
      borderRadius: {
        control: "var(--ui-radius-control)",
        panel: "var(--ui-radius-panel)",
        dialog: "var(--ui-radius-dialog)",
      },
      boxShadow: {
        panel: "var(--ui-shadow-panel)",
        popover: "var(--ui-shadow-popover)",
        dialog: "var(--ui-shadow-dialog)",
      },
      transitionDuration: {
        fast: "var(--ui-duration-fast)",
        ui: "var(--ui-duration)",
      },
      fontSize: {
        "ui-meta": ["12px", "16px"],
        ui: ["14px", "20px"],
        "ui-body": ["16px", "24px"],
        "ui-section": ["18px", "24px"],
        "ui-title": ["24px", "32px"],
      },
      zIndex: {
        sticky: "10",
        dropdown: "20",
        "modal-backdrop": "30",
        modal: "40",
        toast: "50",
      },
    },
  },
} satisfies Partial<Config>;
