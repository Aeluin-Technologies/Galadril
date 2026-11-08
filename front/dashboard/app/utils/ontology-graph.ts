import type { OntologyResource, SemanticDocument } from "../types/ontology";

/** A single graph projection keeps live and historical models consistent. */
export function ontologyGraph(
  document: SemanticDocument | undefined,
  selection = "",
  label = (resource: OntologyResource) => resource.display_name,
  expandedPackages?: ReadonlySet<string>,
  packageLabel = (_id: string, name: string) => name,
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
  const labels = new Map(types.map((r) => [r.resource_id, label(r)]));
  const layers = new Map<string, { id: string; name: string }>();
  for (const resource of types) {
    const id = resource.attributes.plugin_package_id;
    if (typeof id === "string")
      layers.set(resource.resource_id, {
        id,
        name:
          typeof resource.attributes.plugin_package_name === "string"
            ? resource.attributes.plugin_package_name
            : id,
      });
  }
  const selectedLayer = layers.get(selected)?.id;
  const folded = (id: string) => {
    const layer = layers.get(id);
    return layer &&
      layer.id !== selectedLayer &&
      !expandedPackages?.has(layer.id)
      ? `package:${layer.id}`
      : id;
  };
  const counts = new Map<string, number>();
  for (const resource of resources)
    if (resource.owner_id)
      counts.set(resource.owner_id, (counts.get(resource.owner_id) ?? 0) + 1);
  const visible = new Map<
    string,
    {
      label: string;
      resourceId: string;
      count: number;
      packageId: string | undefined;
      expanded: boolean;
    }
  >();
  for (const id of ids) {
    if (!labels.has(id)) continue;
    const layer = layers.get(id);
    if (layer) {
      const group = `package:${layer.id}`;
      const current = visible.get(group);
      visible.set(group, {
        label: packageLabel(layer.id, layer.name),
        resourceId: "",
        count: (current?.count ?? 0) + 1,
        packageId: layer.id,
        expanded:
          !!expandedPackages?.has(layer.id) || selectedLayer === layer.id,
      });
    }
    if (folded(id) === id)
      visible.set(id, {
        label: labels.get(id) ?? id,
        resourceId: id,
        count: counts.get(id) ?? 0,
        packageId: undefined,
        expanded: false,
      });
  }
  return {
    nodes: [...visible].map(([id, data], index) => ({
      id,
      selected: id === selected,
      position: {
        x: 40 + (index % 3) * 330,
        y: 40 + Math.floor(index / 3) * 170,
      },
      data,
      style: {
        borderColor: "rgb(var(--ui-border-control))",
        background: "rgb(var(--ui-surface))",
        color: "rgb(var(--ui-foreground))",
      },
    })),
    edges: links
      .filter((r) => {
        const source = folded(String(r.attributes.source_type));
        return (
          !source.startsWith("package:") ||
          source !== folded(String(r.attributes.target_type))
        );
      })
      .map((r) => ({
        id: r.resource_id,
        source: folded(String(r.attributes.source_type)),
        target: folded(String(r.attributes.target_type)),
        label: label(r),
      })),
  };
}
