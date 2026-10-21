export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ControlSize = "sm" | "md" | "lg";
export type Tone = "neutral" | "info" | "success" | "warning" | "danger";
export type ButtonAction = (event: MouseEvent) => void | Promise<void>;
export interface SelectOption<T extends string | number | boolean = string> {
  label: string;
  value: T;
  disabled?: boolean;
  group?: string;
}
