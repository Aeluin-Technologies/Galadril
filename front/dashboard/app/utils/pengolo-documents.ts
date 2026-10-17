import {
  Kind,
  parse,
  print,
  visit,
  type DefinitionNode,
  type FragmentDefinitionNode,
} from "graphql";

/** Send only the chosen operation and its reachable fragments. */
export function pengoloDocuments(source: string): ReadonlyMap<string, string> {
  const document = parse(source);
  const fragments = new Map<string, FragmentDefinitionNode>();
  for (const definition of document.definitions)
    if (definition.kind === Kind.FRAGMENT_DEFINITION)
      fragments.set(definition.name.value, definition);
  const operations = new Map<string, string>();
  for (const definition of document.definitions) {
    if (definition.kind !== Kind.OPERATION_DEFINITION || !definition.name)
      continue;
    const definitions: DefinitionNode[] = [definition];
    const used = new Set<string>();
    const include = (node: DefinitionNode) =>
      visit(node, {
        FragmentSpread(spread) {
          const name = spread.name.value;
          if (used.has(name)) return;
          const fragment = fragments.get(name);
          if (!fragment) throw new Error(`Missing GraphQL fragment: ${name}`);
          used.add(name);
          definitions.push(fragment);
          include(fragment);
        },
      });
    include(definition);
    operations.set(
      definition.name.value,
      print({ kind: Kind.DOCUMENT, definitions }),
    );
  }
  return operations;
}
