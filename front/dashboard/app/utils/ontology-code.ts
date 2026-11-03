import { isMap, isSeq, LineCounter, parseDocument, stringify } from "yaml";
import type { SemanticDocument } from "../types/ontology";

/** YAML ranges keep emphasis aligned with complete resource blocks. */
export function ontologyCodeLines(
  document: SemanticDocument,
  selected: string,
) {
  const source = stringify(document);
  const highlighted = new Set<number>();
  if (selected) {
    const ids = new Set(
      document.resources
        .filter(
          (resource) =>
            resource.resource_id === selected ||
            resource.owner_id === selected ||
            resource.references.includes(selected),
        )
        .map((resource) => resource.resource_id),
    );
    const counter = new LineCounter();
    const parsed = parseDocument(source, { lineCounter: counter });
    const resources = parsed.get("resources", true);
    if (isSeq(resources)) {
      for (const item of resources.items) {
        if (
          !isMap(item) ||
          !item.range ||
          !ids.has(String(item.get("resource_id")))
        )
          continue;
        const first = counter.linePos(item.range[0]).line;
        const last = counter.linePos(item.range[1] - 1).line;
        for (let line = first; line <= last; line++) highlighted.add(line);
      }
    }
  }
  return source.split("\n").map((text, index) => ({
    number: index + 1,
    text,
    highlighted: highlighted.has(index + 1),
  }));
}
