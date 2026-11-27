import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { measureScript, resolveSource, writeLcov } from "./browser-profile.ts";

await test("maps Vue source only when both repository path and bytes agree", () => {
  const panel = {
    path: "front/ui/src/Panel.vue",
    content: "<script>export const value = 1;</script>",
  };
  const identical = { ...panel, path: "front/dashboard/app/Panel.vue" };
  assert.equal(
    resolveSource("../../src/Panel.vue", panel.content, [panel]),
    panel.path,
  );
  assert.equal(
    resolveSource("../../src/Panel.vue", panel.content + "\n", [panel]),
    undefined,
  );
  assert.equal(
    resolveSource("Panel.vue", panel.content, [panel, identical]),
    undefined,
  );
  assert.equal(
    resolveSource("../../vendor/Panel.vue", panel.content, [panel]),
    undefined,
  );
});

const code = "function hit() { return 1; }\nfunction miss() { return 0; }\n";
const vue = "<script setup>\n" + code + "</script>\n";
const url = "http://localhost/_nuxt/chunk.js";
const functions = [
  {
    functionName: "",
    isBlockCoverage: true,
    ranges: [{ startOffset: 0, endOffset: code.length, count: 1 }],
  },
  {
    functionName: "hit",
    isBlockCoverage: true,
    ranges: [{ startOffset: 0, endOffset: code.indexOf("\n"), count: 1 }],
  },
  {
    functionName: "miss",
    isBlockCoverage: false,
    ranges: [
      {
        startOffset: code.indexOf("\n") + 1,
        endOffset: code.length - 1,
        count: 0,
      },
    ],
  },
];

function map(name: string, content: string, mappings: string) {
  return {
    version: 3,
    sources: [name],
    sourcesContent: [content],
    names: [],
    mappings,
  };
}

await test("emits covered and uncovered functions from the same Chromium script", async () => {
  const path = "front/ui/src/example.ts";
  const lcov = await fixtureLcov(
    path,
    code,
    "AAAA,iBAAiB;AACjB,iBAAiB",
    functions,
  );
  assert.match(lcov, /FNDA:1,hit/);
  assert.match(lcov, /FNDA:0,miss/);
  assert.match(lcov, /DA:1,1/);
  assert.match(lcov, /DA:2,0/);
});

await test("counts actual executable statements in a never loaded emitted module", async () => {
  const path = "front/ui/src/example.ts";
  const lcov = await fixtureLcov(path, code, "AAAA,iBAAiB;AACjB,iBAAiB", []);
  assert.match(lcov, /FNDA:0,hit/);
  assert.match(lcov, /FNDA:0,miss/);
  assert.match(lcov, /DA:1,0/);
  assert.match(lcov, /DA:2,0/);
  assert.match(lcov, /LF:2/);
});

await test("an invalid source map leaves the script non-measured", async () => {
  const path = "front/ui/src/example.ts";
  const result = await measureScript(
    { code, url, functions },
    map(path, code, "AA+//////BA"),
    [{ path, content: code }],
    "/workspace",
  );
  assert.equal(result.status, "missing");
});

await test("emits the original Vue path and function lines", async () => {
  const path = "front/ui/src/Panel.vue";
  const lcov = await fixtureLcov(
    path,
    vue,
    "AACA,iBAAiB;AACjB,iBAAiB",
    functions,
  );
  assert.match(lcov, /^SF:front\/ui\/src\/Panel\.vue$/m);
  assert.match(lcov, /^FN:2,hit$/m);
  assert.match(lcov, /^DA:3,0$/m);
  assert.doesNotMatch(lcov, /^DA:1,/m);
});

async function fixtureLcov(
  path: string,
  content: string,
  mappings: string,
  profile: typeof functions,
): Promise<string> {
  const result = await measureScript(
    { code, url, functions: profile },
    map(path, content, mappings),
    [{ path, content }],
    "/workspace",
  );
  assert.equal(result.status, "measured", JSON.stringify(result));
  const directory = mkdtempSync(join(tmpdir(), "galadril-coverage-test-"));
  try {
    writeLcov(directory, "fixture.dat", "/workspace", [result.data]);
    return readFileSync(join(directory, "fixture.dat"), "utf8");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

await test("an invalid Vue template column does not erase valid script points", async () => {
  const path = "front/ui/src/Panel.vue";
  const content = vue + "<template>\n  <div />\n</template>\n";
  const result = await measureScript(
    { code, url, functions },
    map(path, content, "AACA,iBAAiB;AAIuB,iBAAE"),
    [{ path, content }],
    "/workspace",
  );
  assert.equal(result.status, "measured", JSON.stringify(result));
  assert.equal(result.unmappedTemplateSegments, 2);
  const mapped = result.data["/workspace/" + path];
  assert.ok(mapped);
  assert.deepEqual(
    Object.values(mapped.fnMap).map((item) => item.decl.start.line),
    [2],
  );
});
