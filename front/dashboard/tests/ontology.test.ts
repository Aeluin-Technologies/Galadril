import type { SemanticDocument } from "../app/types/ontology";
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  parseSemanticDocument,
  parseOntologyWorkspace,
  parseOntologyHistory,
  parseSemanticPackages,
  searchOntologies,
  ontologyPrimaryAction,
  withOntologyLink,
  sameOntologyDefinition,
  officialOntologyResourceKey,
} from "../app/utils/ontology.ts";
import { ontologyCodeLines } from "../app/utils/ontology-code.ts";
import { ontologyGraph } from "../app/utils/ontology-graph.ts";
import {
  withOntologyType,
  withUpdatedOntologyLink,
  withoutOntologyLink,
} from "../app/utils/ontology-edit.ts";

test("imported layers fold as one block and expand without changing the model", () => {
  const document: SemanticDocument = {
    version: "1",
    resources: [
      {
        resource_id: "sample.party",
        kind: "object_type",
        display_name: "Party",
        references: [],
        attributes: {
          plugin_package_id: "sample.core",
          plugin_package_name: "Shared foundations",
        },
      },
      {
        resource_id: "sample.court",
        kind: "object_type",
        display_name: "Court",
        references: [],
        attributes: {
          plugin_package_id: "sample.core",
          plugin_package_name: "Shared foundations",
        },
      },
      {
        resource_id: "sample.writing",
        kind: "object_type",
        display_name: "Écrit",
        references: [],
        attributes: {},
      },
      {
        resource_id: "sample.author",
        kind: "link_type",
        display_name: "Author",
        references: ["sample.writing", "sample.party"],
        attributes: {
          source_type: "sample.writing",
          target_type: "sample.party",
        },
      },
    ],
  };
  const before = JSON.stringify(document);
  const graph = ontologyGraph(document);
  assert.equal(graph.nodes.length, 2);
  assert.equal(
    graph.nodes.find((node) => node.id === "package:sample.core")?.data
      .packageId,
    "sample.core",
  );
  assert.equal(graph.edges[0]?.target, "package:sample.core");
  const expanded = ontologyGraph(
    document,
    "",
    (resource) => resource.display_name,
    new Set(["sample.core"]),
  );
  assert.ok(expanded.nodes.some((node) => node.id === "sample.party"));
  assert.equal(expanded.edges[0]?.target, "sample.party");
  assert.equal(JSON.stringify(document), before);
});

test("catalogue imports retain layer provenance before materialization", () => {
  const packages = parseSemanticPackages({
    packages: [
      {
        id: "sample.branch",
        plugin_id: "sample",
        plugin_version: "1",
        version: "1",
        name: "Branch",
        ontologies: [
          {
            id: "sample.core",
            package_id: "sample.core",
            package_name: "Shared foundations",
            resources: [
              {
                resource_id: "sample.party",
                kind: "object_type",
                display_name: "Party",
                references: [],
                attributes: {},
              },
            ],
          },
        ],
      },
    ],
  });
  const resources = packages[0]!.ontologies[0]!.resources;
  assert.equal(resources[0]!.attributes.plugin_package_id, "sample.core");
  assert.equal(
    ontologyGraph({ version: "1", resources }).nodes[0]?.data.label,
    "Shared foundations",
  );
});

test("official ontology labels exclude tenant and imported resources", () => {
  const resource = {
    resource_id: "law.brief",
    kind: "object_type",
    display_name: "Mémoire",
    references: [],
    attributes: {},
  };
  const packaged = {
    ...resource,
    attributes: { plugin_module: "law.ecritures" },
  };
  assert.equal(
    officialOntologyResourceKey(resource, "law.contentieux"),
    undefined,
  );
  assert.equal(officialOntologyResourceKey(packaged, undefined), undefined);
  assert.equal(officialOntologyResourceKey(packaged, "utils.mail"), undefined);
  assert.equal(
    officialOntologyResourceKey(packaged, "law.contentieux"),
    "officialOntologies.law_contentieux.resources.law_brief",
  );
  assert.equal(
    officialOntologyResourceKey(resource, "law.contentieux", true),
    "officialOntologies.law_contentieux.resources.law_brief",
  );
});
test("object creation validates its definition and keeps existing resources immutable", () => {
  const document = { version: "1", resources: [] };
  const input = {
    id: "tenant.meeting",
    name: "Meeting",
    definition: "A scheduled meeting",
    kind: "event_type" as const,
    example: "Weekly review",
  };
  const result = withOntologyType(document, input);
  assert.equal(document.resources.length, 0);
  assert.equal(result.resources[0]?.kind, "event_type");
  assert.equal(result.resources[0]?.attributes.description, input.definition);
  assert.equal(result.resources[0]?.attributes.example, input.example);
  assert.throws(() => withOntologyType(result, input));
  assert.throws(() =>
    withOntologyType(document, { ...input, definition: " " }),
  );
  assert.throws(() =>
    withOntologyType(document, { ...input, id: "../invalid" }),
  );
});
test("relationship edits preserve metadata and reject dangling or protected links", () => {
  const types = ["a", "b", "c"].map((id) => ({
    resource_id: id,
    kind: "object_type",
    display_name: id,
    references: [],
    attributes: {},
  }));
  const document = withOntologyLink(
    { version: "1", resources: types },
    "link.ab",
    "a",
    "b",
  );
  const changed = withUpdatedOntologyLink(
    document,
    "link.ab",
    "c",
    "b",
    "Works with",
  );
  assert.deepEqual(changed.resources.at(-1)?.references, ["c", "b"]);
  assert.equal(changed.resources.at(-1)?.display_name, "Works with");
  assert.equal(document.resources.at(-1)?.display_name, "Relationship");
  assert.throws(() =>
    withUpdatedOntologyLink(document, "a", "a", "b", "Invalid"),
  );
  assert.throws(() =>
    withUpdatedOntologyLink(document, "link.ab", "missing", "b", "Invalid"),
  );
  assert.equal(withoutOntologyLink(document, "link.ab").resources.length, 3);
  assert.throws(() => withoutOntologyLink(document, "a"));
  assert.throws(() =>
    withoutOntologyLink(
      {
        ...document,
        resources: [
          ...document.resources,
          { ...types[0]!, resource_id: "dependent", references: ["link.ab"] },
        ],
      },
      "link.ab",
    ),
  );
});
test("ontology code highlights the selected type, owned fields and connected links", () => {
  const type = {
    resource_id: "person",
    kind: "object_type",
    display_name: "Person",
    references: [],
    attributes: {},
  };
  const document = {
    version: "1",
    resources: [
      { ...type, resource_id: "message", display_name: "Message" },
      type,
      {
        ...type,
        resource_id: "person.name",
        kind: "property",
        owner_id: "person",
        value_type: "string",
        display_name: "Name",
      },
      {
        ...type,
        resource_id: "sender",
        kind: "link_type",
        references: ["message", "person"],
      },
    ],
  };
  const highlighted = ontologyCodeLines(document, "person")
    .filter((line) => line.highlighted)
    .map((line) => line.text)
    .join("\n");
  assert.match(highlighted, /resource_id: person/);
  assert.match(highlighted, /resource_id: person.name/);
  assert.match(highlighted, /resource_id: sender/);
  assert.doesNotMatch(highlighted, /resource_id: message/);
  assert.equal(
    ontologyCodeLines(document, "").some((line) => line.highlighted),
    false,
  );
  assert.equal(
    ontologyCodeLines(document, "missing").some((line) => line.highlighted),
    false,
  );
});
test("publication status compares semantic content across tenant metadata commits", () => {
  const doc = {
    version: "1",
    resources: ["a", "b"].map((id) => ({
      resource_id: id,
      kind: "object_type",
      display_name: id,
      references: [],
      attributes: { description: "A type", visible: true },
    })),
  };
  assert.equal(sameOntologyDefinition(doc, undefined), false);
  assert.equal(
    sameOntologyDefinition(doc, {
      ...doc,
      resources: [...doc.resources].reverse().map((resource) => ({
        ...resource,
        attributes: { visible: true, description: "A type" },
      })),
    }),
    true,
  );
  assert.equal(sameOntologyDefinition(doc, { ...doc, version: "2" }), false);
  assert.equal(sameOntologyDefinition(doc, { ...doc, resources: [] }), false);
});
test("ontology lifecycle presents one action for the current draft", () => {
  assert.equal(
    ontologyPrimaryAction({
      revision: undefined,
      dirty: false,
      published: false,
    }),
    "review",
  );
  assert.equal(
    ontologyPrimaryAction({ revision: "r1", dirty: true, published: true }),
    "review",
  );
  assert.equal(
    ontologyPrimaryAction({ revision: "r1", dirty: false, published: false }),
    "publish",
  );
  assert.equal(
    ontologyPrimaryAction({ revision: "r1", dirty: false, published: true }),
    "edit",
  );
});
test("graph edits cannot link missing resources or rewrite a plugin original", () => {
  const doc = {
    version: "1.0.0",
    resources: ["a", "b"].map((id) => ({
      resource_id: id,
      kind: "object_type",
      display_name: id,
      references: [],
      attributes: {},
    })),
  };
  const result = withOntologyLink(doc, "link.ab", "a", "b");
  assert.equal(doc.resources.length, 2);
  assert.equal(result.resources.length, 3);
  assert.deepEqual(result.resources[2]?.references, ["a", "b"]);
  assert.throws(() => withOntologyLink(doc, "link.ab", "a", "missing"));
  assert.throws(() => withOntologyLink(result, "link.ab", "a", "b"));
  assert.throws(() =>
    withOntologyLink(
      {
        ...doc,
        plugin_origin: {
          package_id: "utils.mail",
          package_version: "1",
          plugin_version: "1",
        },
      },
      "link.ab",
      "a",
      "b",
    ),
  );
});
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

test("workspace branches preserve isolated definitions and bound untrusted metadata", () => {
  const model = { version: "1", resources: [] };
  const branch = {
    parent_branch: "main",
    definition: model,
    display_name: "Draft",
    base_revision_id: "a".repeat(32),
    author: "editor",
    updated_at_ms: 12,
  };
  const workspace = parseOntologyWorkspace({
    revision_id: "b".repeat(32),
    ontology: model,
    authoring: { branches: { experiment: branch }, deleted_at_ms: null },
  });
  assert.equal(workspace.authoring.branches.experiment?.display_name, "Draft");
  assert.equal(workspace.authoring.deleted_at_ms, null);
  assert.deepEqual(
    parseOntologyWorkspace({ revision_id: "a".repeat(32), ontology: model })
      .authoring.branches,
    {},
  );
  assert.throws(() =>
    parseOntologyWorkspace({
      revision_id: "a".repeat(32),
      ontology: model,
      authoring: {
        branches: Object.fromEntries(
          Array.from({ length: 21 }, (_, i) => [`b${i}`, branch]),
        ),
      },
    }),
  );
  assert.throws(() =>
    parseOntologyWorkspace({
      revision_id: "a".repeat(32),
      ontology: model,
      authoring: { branches: { main: branch } },
    }),
  );
});
test("history pagination rejects malformed entries and oversized pages", () => {
  const entry = {
    revision_id: "a".repeat(32),
    author: "editor",
    message: "Draft saved",
    branch: "main",
    operation: "save_draft",
    created_at_ms: 12,
  };
  const page = parseOntologyHistory({
    items: [entry],
    next_revision_id: "b".repeat(32),
  });
  assert.equal(page.items[0]?.branch, "main");
  assert.equal(page.next_revision_id, "b".repeat(32));
  assert.throws(() =>
    parseOntologyHistory({ items: [entry], next_revision_id: "../invalid" }),
  );
  assert.throws(() =>
    parseOntologyHistory({ items: Array.from({ length: 101 }, () => entry) }),
  );
});
