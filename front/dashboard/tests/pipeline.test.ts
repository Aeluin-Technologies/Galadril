import assert from "node:assert/strict";
import { test } from "node:test";
import {
  copyConfig,
  graphFromDefinition,
  definitionFromGraph,
  validateConnections,
} from "../app/utils/pipeline.ts";
import type { GraphNode } from "../app/utils/pipeline.ts";

test("stored pipelines round-trip both parallel sinks and route outcomes", () => {
  const definition = {
    name: "Correspondence",
    sources: [
      {
        id: "mail",
        topic: "raw",
        match_pattern: ".*",
        schema_path: "schemas/avro/email.avsc",
        source_kind: "email",
        parser: "json",
        group_id: "email",
      },
    ],
    pipeline: [
      {
        step: "choose",
        type: "route",
        routing: "metadata",
        input_from: ["mail"],
        params: { field: "sender" },
        branches: { match: ["graph", "retrieval"], other: ["ignore"] },
      },
      { step: "graph", type: "sink", input_from: ["choose"], params: {} },
      {
        step: "retrieval",
        type: "sink",
        capability_id: "core.output.hipporag2",
        capability_version: "1.0.0",
        input_from: ["choose"],
        params: {},
      },
      { step: "ignore", type: "sink", input_from: ["choose"], params: {} },
    ],
  };
  const graph = graphFromDefinition(definition);
  const restored = definitionFromGraph(
    definition.name,
    graph.nodes,
    graph.edges,
  );
  assert.deepEqual(
    restored.pipeline[0]?.branches,
    definition.pipeline[0]?.branches,
  );
  assert.equal(restored.pipeline[2]?.capability_id, "core.output.hipporag2");
  assert.equal(restored.sources[0]?.source_kind, "email");
  assert.equal(graph.edges.length, 4);
  assert.throws(() =>
    graphFromDefinition({
      ...definition,
      pipeline: [...definition.pipeline, definition.pipeline[0]!],
    }),
  );
});

test("routing exports labeled branches and immutable pipeline references", () => {
  const nodes: GraphNode[] = [
    {
      id: "input",
      data: {
        stepType: "source",
        config: { topic: "raw", match_pattern: ".*", schema_path: "text.avsc" },
      },
    },
    {
      id: "choose",
      data: {
        stepType: "route",
        config: {
          routing: "regex",
          params: {
            field: "record.raw_payload.content",
            pattern: "appeal",
            match_branch: "legal",
            default_branch: "other",
          },
        },
      },
    },
    {
      id: "send",
      data: {
        stepType: "pipeline",
        config: {
          pipeline_ref: {
            pipeline_id: "law",
            revision_id: "a".repeat(32),
            input_source: "mail",
          },
        },
      },
    },
    { id: "other", data: { stepType: "sink", config: { params: {} } } },
  ];
  const edges = [
    { source: "input", target: "choose" },
    { source: "choose", target: "send", sourceHandle: "legal" },
    { source: "choose", target: "other", sourceHandle: "other" },
  ];
  const result = definitionFromGraph("example", nodes, edges);
  assert.deepEqual(
    result.pipeline.find((step) => step.step === "choose")?.branches,
    { legal: ["send"], other: ["other"] },
  );
  assert.equal(
    result.pipeline.find((step) => step.step === "send")?.pipeline_ref
      ?.revision_id,
    "a".repeat(32),
  );
});

test("connections reject cycles, incoming source edges, and transfer continuations", () => {
  const nodes = [
    { id: "a", data: { stepType: "source", config: {} } },
    { id: "b", data: { stepType: "sink", config: {} } },
    { id: "c", data: { stepType: "pipeline", config: {} } },
  ];
  assert.throws(() =>
    validateConnections(nodes, [
      { source: "a", target: "b" },
      { source: "b", target: "a" },
    ]),
  );
  assert.throws(() =>
    validateConnections(nodes, [{ source: "c", target: "b" }]),
  );
});

test("the editor limits transfers rather than ordinary local branches", () => {
  const source = { id: "input", data: { stepType: "source", config: {} } };
  const local = Array.from({ length: 4 }, (_, index) => ({
    id: `node${index}`,
    data: { stepType: "sink", config: {} },
  }));
  validateConnections(
    [source, ...local],
    local.map((node) => ({ source: "input", target: node.id })),
  );
  const transfers = local.map((node) => ({
    ...node,
    data: { ...node.data, stepType: "pipeline" },
  }));
  assert.throws(() => validateConnections([source, ...transfers], []));
});

test("branch labels serialize as own keys even when they match prototype names", () => {
  const nodes = [
    { id: "input", data: { stepType: "source", config: {} } },
    {
      id: "route",
      data: { stepType: "route", config: { branch_labels: ["__proto__"] } },
    },
    { id: "output", data: { stepType: "sink", config: {} } },
  ];
  const definition = definitionFromGraph("safe", nodes, [
    { source: "input", target: "route" },
    { source: "route", target: "output", sourceHandle: "__proto__" },
  ]);
  const branches = definition.pipeline.find(
    (step) => step.step === "route",
  )?.branches;
  assert.ok(branches && Object.hasOwn(branches, "__proto__"));
  assert.deepEqual(JSON.parse(JSON.stringify(branches)), {
    ["__proto__"]: ["output"],
  });
});

test("editing copies nested reactive parameters without retaining proxies", () => {
  const original = {
    params: { conditions: new Proxy({ title: "Appeal" }, {}) },
  };
  const copied = copyConfig(original);
  assert.doesNotThrow(() => structuredClone(copied));
  assert.deepEqual(copied.params, { conditions: { title: "Appeal" } });
  assert.notEqual(copied.params?.conditions, original.params.conditions);
});

test("plugin sources and handlers retain deployment version pins in exports", () => {
  const result = definitionFromGraph(
    "plugins",
    [
      {
        id: "input",
        data: {
          stepType: "source",
          config: {
            capability_id: "fixture.input.text",
            capability_version: "1.2.0",
          },
        },
      },
      {
        id: "count",
        data: {
          stepType: "algorithm",
          config: {
            capability_id: "fixture.algorithm.count",
            capability_version: "1.2.0",
            params: { limit: 2 },
          },
        },
      },
    ],
    [{ source: "input", target: "count" }],
  );
  assert.equal(result.sources[0]?.capability_id, "fixture.input.text");
  assert.equal(result.sources[0]?.capability_version, "1.2.0");
  assert.equal(result.pipeline[0]?.capability_version, "1.2.0");
});

test("plugin authoring filters executable blocks, seeds parameters, and preserves pins", async () => {
  const { pluginBlocks, pluginConfig, parseParameters } =
    await import("../app/utils/plugin-blocks.ts");
  const capability = {
    id: "fixture.algorithm.count",
    pluginId: "fixture",
    pluginVersion: "1.0.0",
    origin: "PLUGIN",
    execution: "REGISTERED",
    kind: "ALGORITHM",
    targets: ["PIPELINE"],
    name: "Count",
    description: "Count records",
    stepType: "algorithm",
    parametersSchemaJson: JSON.stringify({
      type: "object",
      properties: { limit: { type: "integer", default: 2 } },
      required: ["limit"],
    }),
    compatibility: { allowedModels: [], requires: [] },
  };
  assert.equal(
    pluginBlocks([
      capability,
      { ...capability, id: "fixture.model.text", targets: ["SCRIBE"] },
      { ...capability, stepType: "route" },
    ]).length,
    1,
  );
  assert.deepEqual(pluginConfig(capability), {
    capability_id: capability.id,
    capability_version: "1.0.0",
    params: { limit: 2 },
  });
  assert.equal(
    pluginConfig({
      ...capability,
      compatibility: { allowedModels: ["local-model"], requires: [] },
    }).model,
    "local-model",
  );
  assert.deepEqual(parseParameters('{"limit":3}'), { limit: 3 });
  assert.throws(() => parseParameters("[]"));
  assert.throws(() => parseParameters('{"bad":1e999}'));
});
