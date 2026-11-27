import assert from "node:assert/strict";
import { test } from "node:test";
import type { GolSummary } from "../app/types/ontology.ts";
import {
  parseGolWorkspace,
  parseGolSymbols,
  parseGolValidation,
  diagnosticPosition,
  searchOntologies,
  sourceChanged,
  golDraftSource,
} from "../app/utils/ontology.ts";

const summary: GolSummary = {
  ontologyId: "communication.mail",
  displayName: "Mail",
  sourceRevision: "revision-1",
  activeGeneration: "generation-1",
  activeSourceRevision: "revision-1",
  status: "published",
  origin: null,
};
const text =
  'ontology communication.mail version "1.0.0" {\n  entity Message { field title: String; }\n}';

test("GOL workspaces retain source text and immutable publication identity", () => {
  const value = parseGolWorkspace({
    summary,
    files: [{ path: "mail.gol", text }],
    diagnostics: [],
    dependencies: [],
  });
  assert.equal(value.files[0]?.text, text);
  assert.equal(value.summary.activeGeneration, "generation-1");
  assert.equal(sourceChanged(value.files, [{ path: "mail.gol", text }]), false);
  assert.equal(
    sourceChanged(value.files, [{ path: "mail.gol", text: `${text}\n` }]),
    true,
  );
});

test("syntax errors remain editable draft diagnostics without a client parser", () => {
  const diagnostic = {
    code: "GOL_SYNTAX",
    message: "Expected closing brace",
    path: "mail.gol",
    start: 3,
    end: 4,
    severity: "error",
  };
  const validation = parseGolValidation({
    ontologyId: "communication.mail",
    sourceRevision: "revision-2",
    status: "invalid",
    diagnostics: [diagnostic],
    dependencies: [],
  });
  assert.equal(validation.status, "invalid");
  assert.equal(validation.diagnostics[0]?.code, "GOL_SYNTAX");
  assert.throws(() =>
    parseGolValidation({ ...validation, status: "approved" }),
  );
  assert.throws(() =>
    parseGolWorkspace({
      summary,
      files: [{ path: "../secret.gol", text }],
      diagnostics: [],
      dependencies: [],
    }),
  );
});

test("UTF-8 compiler spans map to editor positions without splitting Unicode", () => {
  const source = "αβ\n  title";
  assert.deepEqual(diagnosticPosition(source, 7, 12), {
    start: 5,
    end: 10,
    line: 2,
    column: 3,
  });
  assert.deepEqual(diagnosticPosition(source, 1, 3), {
    start: 0,
    end: 1,
    line: 1,
    column: 1,
  });
  assert.deepEqual(diagnosticPosition(source, 999, 1000), {
    start: source.length,
    end: source.length,
    line: 2,
    column: 8,
  });
});

test("exploration accepts only authorized symbol projections pinned to one generation", () => {
  const projection = parseGolSymbols({
    generation: "generation-1",
    symbols: [
      {
        key: "communication.mail::Message@1.0.0",
        name: "Message",
        kind: "entity",
        references: [],
        properties: [
          { key: "Message.title", name: "title", typeName: "String" },
        ],
      },
    ],
  });
  assert.equal(projection.symbols[0]?.properties[0]?.name, "title");
  assert.throws(() => parseGolSymbols({ generation: "", symbols: [] }));
  assert.throws(() =>
    parseGolSymbols({
      generation: "generation-1",
      symbols: [
        {
          key: "x",
          name: "x",
          kind: "entity",
          references: [17],
          properties: [],
        },
      ],
    }),
  );
  assert.throws(() =>
    parseGolWorkspace({ revision_id: "legacy", ontology: { resources: [] } }),
  );
});

test("catalog search links authorized GOL ontologies without legacy semantic records", () => {
  const hits = searchOntologies("mail", [summary], "Ontologies");
  assert.equal(hits[0]?.href, "/ontology/edit/communication.mail");
  assert.equal(hits[0]?.label, "Mail");
});

test("GOL starter sources declare explicit host contracts without reserved permission names", () => {
  const source = golDraftSource("tenant.example");
  assert.match(source, /ontology tenant\.example version "1.0.0"/);
  assert.match(source, /permission read_access/);
  assert.match(source, /permission write_access/);
  assert.match(source, /permission describe/);
  assert.match(source, /permission transfer/);
  assert.doesNotMatch(source, /permission (?:read|write)\s*\{/);
});
