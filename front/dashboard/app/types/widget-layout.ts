import type { Component } from "vue";

/** Dashboard export data includes presentation options and an in-memory icon. */
export interface WidgetConfig {
  title?: string;
  value?: string;
  trend?: string;
  icon?: Component;
  data?: number[];
  categories?: string[];
  alerts?: unknown[];
  items?: unknown[];
}

export interface WidgetLayout {
  x: number;
  y: number;
  w: number;
  h: number;
  i: string;
  type: string;
  config: WidgetConfig;
}
