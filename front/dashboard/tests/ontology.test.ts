import assert from "node:assert/strict";
import { test } from "node:test";
import {
  parseSemanticDocument,
  parseSemanticPackages,
} from "../app/utils/ontology.ts";
test("semantic wire input is bounded and parsed without trusting type assertions", () => {
  const document = {
    version: "1",
    resources: [
      {
        resource_id: "tenant.person",
        kind: "object_type",
        display_name: "Person",
        references: [],
        attributes: {},
      },
    ],
  };
  assert.deepEqual(parseSemanticDocument(JSON.stringify(document)), document);
  for (const bad of [
    {
      ...document,
      resources: [{ ...document.resources[0], kind: "executable" }],
    },
    {
      ...document,
      resources: [{ ...document.resources[0], references: "wrong" }],
    },
    { ...document, version: 1 },
    null,
  ])
    assert.throws(() => parseSemanticDocument(bad));
  assert.throws(() =>
    parseSemanticPackages({ packages: [{ id: "utils.mail", ontologies: [] }] }),
  );
});
