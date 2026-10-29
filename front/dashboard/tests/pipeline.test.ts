import assert from "node:assert/strict";
import { test } from "node:test";
import {
  copyConfig,
  graphFromDefinition,
  definitionFromGraph,
  validateConnections,
} from "../app/utils/pipeline.ts";
import type { GraphNode } from "../app/utils/pipeline.ts";
import { sourceConfig } from "../app/utils/plugin-blocks.ts";

test("email mailbox choice survives persistence and scopes incoming events", () => {
  const account = "a".repeat(32);
  const config = sourceConfig(
    { capability_id: "utils.input.email" },
    { account_id: account },
  );
  assert.equal(config.account_id, account);
  assert.equal(
    new RegExp(config.match_pattern ?? "").test(
      `email-${account}-${"c".repeat(32)}.json`,
    ),
    true,
  );
  assert.equal(
    new RegExp(config.match_pattern ?? "").test(
      `email-${"b".repeat(32)}-${"c".repeat(32)}.json`,
    ),
    false,
  );
  const result = graphFromDefinition(
    definitionFromGraph(
      "Mail",
      [{ id: "input", data: { stepType: "source", config } }],
      [],
    ),
  );
  assert.equal(result.nodes[0]?.data.config.account_id, account);
});

test("stored outputs keep their installed names and identify unavailable versions", () => {
  const definition = {
    name: "Mail",
    sources: [
      {
        id: "input",
        topic: "raw",
        match_pattern: ".*",
        schema_path: "email.avsc",
      },
    ],
    pipeline: [
      {
        step: "memory",
        type: "sink",
        input_from: ["input"],
        capability_id: "core.output.hipporag2",
        capability_version: "1.0.0",
        params: {},
      },
    ],
  };
  const catalog = [
    {
      id: "core.output.hipporag2",
      pluginVersion: "1.0.0",
      name: "HippoRAG2 evidence memory",
    },
  ];
  assert.equal(
    graphFromDefinition(definition, catalog).nodes[1]?.data.title,
    "HippoRAG2 evidence memory",
  );
  assert.equal(
    graphFromDefinition(
      {
        ...definition,
        pipeline: [
          { ...definition.pipeline[0], capability_version: "missing" },
        ],
      },
      catalog,
    ).nodes[1]?.data.title,
    "core.output.hipporag2",
  );
});

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

test("incomplete drafts keep their layout and disconnected blocks", async () => {
  const { draftFromGraph, diagnoseGraph } =
    await import("../app/utils/pipeline.ts");
  const nodes = [
    {
      id: "draft",
      position: { x: -120, y: 480 },
      data: { stepType: "inference", config: {} },
    },
  ];
  const draft = draftFromGraph("Unfinished", nodes, []);
  assert.deepEqual(draft.editor?.positions.draft, { x: -120, y: 480 });
  assert.deepEqual(draft.pipeline[0]?.input_from, []);
  assert.deepEqual(graphFromDefinition(draft).nodes[0]?.position, {
    x: -120,
    y: 480,
  });
  assert.ok(
    diagnoseGraph(nodes, []).some(
      (issue) => issue.nodeId === "draft" && issue.code === "input",
    ),
  );
  assert.throws(() => definitionFromGraph("Unfinished", nodes, []));
});

test("unions accept several event streams while ordinary blocks reject joins", () => {
  const inputs = ["a", "b"].map((id) => ({
    id,
    data: { stepType: "source", config: {} },
  }));
  const edges = inputs.map((node) => ({ source: node.id, target: "union" }));
  const union = {
    id: "union",
    data: { stepType: "forward", config: { params: { merge: "union" } } },
  };
  assert.doesNotThrow(() => validateConnections([...inputs, union], edges));
  assert.equal(
    definitionFromGraph("Union", [...inputs, union], edges).pipeline[0]
      ?.input_from.length,
    2,
  );
  assert.throws(() =>
    validateConnections(
      [...inputs, { ...union, data: { stepType: "sink", config: {} } }],
      edges,
    ),
  );
});

test("business conditions compile recipient and subject rules without user-authored paths", async () => {
  const { conditionParams } =
    await import("../app/utils/pipeline-conditions.ts");
  const result = conditionParams("filter", [
    { field: "recipient", operator: "equals", value: "help@example.org" },
    { field: "subject", operator: "contains", value: "Invoice" },
  ]);
  assert.equal(result.mode, "filter");
  assert.deepEqual(result.conditions, [
    { field: "recipient", operator: "equals", value: "help@example.org" },
    { field: "subject", operator: "contains", value: "Invoice" },
  ]);
  assert.throws(() =>
    conditionParams("filter", [
      { field: "subject", operator: "contains", value: "" },
    ]),
  );
});

test("connection repair is a reviewable proposal that never mutates the graph", async () => {
  const { proposeConnectionRepair } = await import("../app/utils/pipeline.ts");
  const nodes = [
    { id: "a", data: { stepType: "source", config: {} } },
    { id: "b", data: { stepType: "sink", config: {} } },
  ];
  const edges = [
    { source: "a", target: "b" },
    { source: "a", target: "b" },
  ];
  const proposal = proposeConnectionRepair(nodes, edges);
  assert.equal(edges.length, 2);
  assert.equal(proposal.removed.length, 1);
  assert.equal(proposal.edges.length, 1);
});

test("catalog forms preserve business help and hide fixed source implementation settings", async () => {
  const { parameterFields } = await import("../app/utils/plugin-blocks.ts");
  const fields = parameterFields({
    id: "mail",
    pluginId: "utils",
    pluginVersion: "1.0.0",
    origin: "PLUGIN",
    execution: "REGISTERED",
    kind: "INPUT",
    targets: ["PIPELINE"],
    name: "Mail",
    description: "",
    stepType: "source",
    compatibility: { allowedModels: [], requires: [] },
    parametersSchemaJson: JSON.stringify({
      type: "object",
      properties: {
        template: {
          type: "string",
          title: "Reply template",
          description: "Use sender and subject variables.",
          format: "multiline",
        },
        parser: {
          type: "string",
          const: "json",
          default: "json",
          "x-advanced": true,
        },
      },
    }),
  });
  assert.equal(fields[0]?.help, "Use sender and subject variables.");
  assert.equal(fields[0]?.format, "multiline");
  assert.equal(fields[1]?.advanced, true);
});

test("typed record ports reject unknown handle identities", () => {
  const nodes = [
    { id: "input", data: { stepType: "source", config: {} } },
    { id: "sink", data: { stepType: "sink", config: {} } },
  ];
  assert.throws(() =>
    validateConnections(nodes, [
      { source: "input", target: "sink", sourceHandle: "embedding" },
    ]),
  );
  assert.throws(() =>
    validateConnections(nodes, [
      { source: "input", target: "sink", targetHandle: "control" },
    ]),
  );
  assert.doesNotThrow(() =>
    validateConnections(nodes, [
      {
        source: "input",
        target: "sink",
        sourceHandle: "record",
        targetHandle: "record",
      },
    ]),
  );
});

test("editing configuration preserves every existing route outcome and union input", async () => {
  const { replaceIncomingConnection } =
    await import("../app/utils/pipeline.ts");
  const edges = [
    { id: "match", source: "route", target: "sink", sourceHandle: "match" },
    { id: "other", source: "route", target: "sink", sourceHandle: "other" },
  ];
  assert.deepEqual(
    replaceIncomingConnection(edges, "sink", "route", "match", false),
    edges,
  );
  const inputs = [
    { id: "a", source: "a", target: "union" },
    { id: "b", source: "b", target: "union" },
  ];
  assert.deepEqual(
    replaceIncomingConnection(inputs, "union", "a", "", true),
    inputs,
  );
});

test("graph arrangement follows dependencies and keeps parallel inputs distinct", async () => {
  const { arrangeGraph } = await import("../app/utils/pipeline.ts");
  const nodes = ["a", "b", "union", "sink"].map((id) => ({
    id,
    data: {
      stepType: id === "a" || id === "b" ? "source" : "forward",
      config: { params: { merge: "union" } },
    },
  }));
  const positions = arrangeGraph(nodes, [
    { source: "a", target: "union" },
    { source: "b", target: "union" },
    { source: "union", target: "sink" },
  ]);
  assert.notEqual(positions.a?.y, positions.b?.y);
  assert.ok((positions.union?.x ?? 0) > (positions.a?.x ?? 0));
  assert.ok((positions.sink?.x ?? 0) > (positions.union?.x ?? 0));
});
