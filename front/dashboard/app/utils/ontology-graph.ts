/** Draw only endpoints already present in the authorized server projection. */
import type { GolSymbol } from "../types/ontology";
export function ontologyGraph(symbols: readonly GolSymbol[], selected = "") {
  const keys = new Set(symbols.map((symbol) => symbol.key));
  return {
    nodes: symbols.map((symbol, index) => ({
      id: symbol.key,
      selected: symbol.key === selected,
      position: {
        x: 40 + (index % 3) * 300,
        y: 40 + Math.floor(index / 3) * 160,
      },
      data: { label: symbol.name, count: symbol.properties.length },
    })),
    edges: symbols.flatMap((symbol) =>
      symbol.references
        .filter((reference) => keys.has(reference))
        .map((reference, index) => ({
          id: `${symbol.key}:${index}`,
          source: symbol.key,
          target: reference,
          label: symbol.kind,
        })),
    ),
  };
}
