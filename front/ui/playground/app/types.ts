export interface Source {
  name: string;
  detail: string;
  kind: string;
  status: "success" | "warning";
  statusLabel: string;
  updated: string;
}
