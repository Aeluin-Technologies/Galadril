import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import { parse } from "vue/compiler-sfc";
import { baseCompile } from "@intlify/message-compiler";
import { record } from "../shared/records.ts";
import type { SFCTemplateBlock } from "vue/compiler-sfc";
type TemplateChildNode = NonNullable<
  SFCTemplateBlock["ast"]
>["children"][number];

const root = resolve(import.meta.dirname, "..");
const folders = ["app/pages", "app/components/Administration"];
const files = folders.flatMap((folder) =>
  readdirSync(resolve(root, folder), { recursive: true, encoding: "utf8" })
    .filter((name) => name.endsWith(".vue"))
    .map((name) => `${folder}/${name}`),
);
files.push(
  ...[
    "Workspace",
    "InputArea",
    "ProjectRow",
    "ProjectEditor",
    "ChatRow",
    "AgentPicker",
  ].map((name) => `app/components/Ai/${name}.vue`),
);
const visibleAttributes = new Set([
  "title",
  "label",
  "placeholder",
  "description",
  "help",
  "tooltip",
  "aria-label",
  "close-label",
]);

test("all locale messages compile as vue-i18n messages", () => {
  const errors: string[] = [];
  function verify(value: unknown, path = "") {
    if (typeof value === "string") {
      baseCompile(value, {
        onError: (error) => errors.push(`${path}: ${error.message}`),
      });
    } else
      for (const [key, child] of Object.entries(record(value)))
        verify(child, `${path}.${key}`);
  }
  verify(JSON.parse(readFileSync(resolve(root, "i18n/en-US.json"), "utf8")));
  assert.deepEqual(errors, []);
});

test("Vue pages and affected features obtain visible copy from nuxt-i18n", () => {
  const failures: string[] = [];
  for (const file of files) {
    const { descriptor } = parse(readFileSync(resolve(root, file), "utf8"));
    function visit(node: TemplateChildNode) {
      if (
        node.type === 1 &&
        ["code", "pre", "script", "style"].includes(node.tag)
      )
        return;
      if (node.type === 2 && /[A-Za-z]{2}/.test(node.content))
        failures.push(`${file}: ${node.content.trim()}`);
      for (const prop of node.type === 1 ? node.props : []) {
        if (
          prop.type === 6 &&
          visibleAttributes.has(prop.name) &&
          /[A-Za-z]{2}/.test(prop.value?.content ?? "")
        )
          failures.push(`${file}: ${prop.name}=${prop.value?.content}`);
      }
      if (node.type === 1) for (const child of node.children) visit(child);
    }
    assert.ok(descriptor.template?.ast);
    for (const child of descriptor.template.ast.children) visit(child);
  }
  assert.deepEqual(failures, []);
});

test("literal translation references resolve in the English catalog", () => {
  const messages = JSON.parse(
    readFileSync(resolve(root, "i18n/en-US.json"), "utf8"),
  );
  for (const file of files) {
    const source = readFileSync(resolve(root, file), "utf8");
    for (const match of source.matchAll(
      /\b\$?t\(\s*["']([a-zA-Z0-9_.-]+)["']/g,
    )) {
      const value = (match[1] ?? "")
        .split(".")
        .reduce<unknown>((object, key) => record(object)[key], messages);
      assert.equal(typeof value, "string", `${file}: ${match[1]}`);
    }
  }
});
