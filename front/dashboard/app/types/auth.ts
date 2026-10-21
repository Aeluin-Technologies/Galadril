export interface IdentityNode {
  type: string;
  attributes: {
    name: string;
    type: string;
    value?: string;
    required?: boolean;
    disabled?: boolean;
  };
  meta: { label?: { text: string } };
}

export interface IdentityForm {
  action: string;
  method: string;
  nodes: IdentityNode[];
  messages?: { text: string; type: string }[];
}
