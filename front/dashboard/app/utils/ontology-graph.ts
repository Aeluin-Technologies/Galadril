import type { SemanticDocument } from "../types/ontology";

/** A single graph projection keeps live and historical models consistent. */
export function ontologyGraph(
  document: SemanticDocument | undefined,
  selection = "",
) {
  const resources = document?.resources ?? [];
  const types = resources.filter((r) =>
    ["object_type", "event_type"].includes(r.kind),
  );
  const selected = types.some((r) => r.resource_id === selection)
    ? selection
    : "";
  const links = resources.filter(
    (r) =>
      r.kind === "link_type" &&
      (!selected ||
        r.attributes.source_type === selected ||
        r.attributes.target_type === selected),
  );
  const ids = new Set([
    ...(selected ? [selected] : types.map((r) => r.resource_id)),
    ...links.flatMap((r) => [
      String(r.attributes.source_type),
      String(r.attributes.target_type),
    ]),
  ]);
  const labels = new Map(types.map((r) => [r.resource_id, r.display_name]));
  const counts = new Map<string, number>();
  for (const resource of resources)
    if (resource.owner_id)
      counts.set(resource.owner_id, (counts.get(resource.owner_id) ?? 0) + 1);
  return {
    nodes: [...ids]
      .filter((id) => labels.has(id))
      .map((id, index) => ({
        id,
        selected: id === selected,
        position: {
          x: 40 + (index % 3) * 330,
          y: 40 + Math.floor(index / 3) * 170,
        },
        data: {
          resourceId: id,
          count: counts.get(id) ?? 0,
          label: labels.get(id) ?? id,
        },
        style: {
          borderColor: "rgb(var(--ui-border-control))",
          background: "rgb(var(--ui-surface))",
          color: "rgb(var(--ui-foreground))",
        },
      })),
    edges: links.map((r) => ({
      id: r.resource_id,
      source: String(r.attributes.source_type),
      target: String(r.attributes.target_type),
      label: r.display_name,
    })),
  };
}
