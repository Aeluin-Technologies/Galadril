import type { Component } from "vue";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ControlSize = "sm" | "md" | "lg";
export type Tone = "neutral" | "info" | "success" | "warning" | "danger";
export type ButtonAction = (event: MouseEvent) => void | Promise<void>;
export interface MenuItem {
  id: string;
  label: string;
  description?: string;
  icon?: Component;
  disabled?: boolean;
  tone?: "default" | "danger";
}
export interface SelectOption<T extends string | number | boolean = string> {
  label: string;
  value: T;
  disabled?: boolean;
  group?: string;
}
