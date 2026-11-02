import assert from "node:assert/strict";
import { test } from "node:test";
import {
  parseSemanticDocument,
  parseSemanticPackages,
  searchOntologies,
} from "../app/utils/ontology.ts";
test("ontology navigation uses authorized catalog entries and canonical local paths", () => {
  const items = [
    {
      ontologyId: "tenant:people.v1",
      displayName: "Organization people",
      productionPublication: null,
    },
  ];
  assert.equal(
    searchOntologies("people", items, "Ontology")[0]?.href,
    "/ontology/edit/tenant%3Apeople.v1",
  );
  assert.deepEqual(searchOntologies("absent", items, "Ontology"), []);
  assert.deepEqual(searchOntologies("people", [], "Ontology"), []);
  assert.deepEqual(searchOntologies("", items, "Ontology"), []);
  assert.deepEqual(searchOntologies(" ", items, "Ontology"), []);
  assert.equal(
    searchOntologies("people".padEnd(512, " "), items, "Ontology").length,
    1,
  );
  assert.deepEqual(
    searchOntologies("people".padEnd(513, " "), items, "Ontology"),
    [],
  );
  assert.equal(
    searchOntologies("ORGANIZATION people", items, "Ontology").length,
    1,
  );
  const matches = Array.from({ length: 10 }, (_, index) => ({
    ontologyId: `people-${index}`,
    displayName: "People",
    productionPublication: null,
  }));
  assert.equal(searchOntologies("people", matches, "Ontology").length, 8);
  assert.equal(
    new Set(
      searchOntologies("people", matches, "Ontology").map((item) => item.id),
    ).size,
    8,
  );
  assert.deepEqual(
    searchOntologies(
      "people",
      [
        {
          ...items[0],
          ontologyId: "//evil",
          displayName: "people",
          productionPublication: null,
        },
      ],
      "Ontology",
    ),
    [],
  );
});
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
