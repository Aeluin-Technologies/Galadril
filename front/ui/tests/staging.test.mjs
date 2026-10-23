import assert from "node:assert/strict";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  symlinkSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";

test("OCI staging materializes workspace siblings and selects the dashboard build", () => {
  const source = readFileSync(
    new URL("../../../build/private/nuxt_pnpm_layer.bzl", import.meta.url),
    "utf8",
  );
  const command = source
    .match(/    command = """\n([\s\S]*?)\n"""/)?.[1]
    ?.replaceAll("\\\\", "\\");
  assert.ok(command);
  const root = mkdtempSync(join(tmpdir(), "galadril-ui-staging-"));
  try {
    mkdirSync(join(root, "input"));
    writeFileSync(join(root, "input/package.json"), '{"name":"@galadril/ui"}');
    symlinkSync(join(root, "input/package.json"), join(root, "ui.json"));
    writeFileSync(
      join(root, "sources.tsv"),
      `${join(root, "ui.json")}\tui/package.json\n${join(root, "ui.json")}\tdashboard/package.json\n`,
    );
    writeFileSync(
      join(root, "docker"),
      [
        "#!/bin/sh",
        "set -eu",
        "found=false",
        'while [ "$#" -gt 0 ]; do',
        '  case "$1" in',
        '    --volume) stage="${2%:/workspace}"; shift ;;',
        '    --env) if [ "$2" = "BUILD_SUBDIR=dashboard" ]; then found=true; fi; shift ;;',
        "  esac",
        "  shift",
        "done",
        'test "$found" = true',
        'test ! -L "$stage/project/ui/package.json"',
        'test -f "$stage/project/dashboard/package.json"',
        'printf artifact > "$stage/layer.tar"',
        "",
      ].join("\n"),
      { mode: 0o755 },
    );
    const result = spawnSync(
      "bash",
      [
        "-c",
        command,
        "test",
        join(root, "sources.tsv"),
        "layer.tar",
        "fixture",
        "linux/amd64",
        "11.6.0",
        "build",
        "src",
        "dashboard",
      ],
      {
        cwd: root,
        env: { ...process.env, PATH: `${root}:${process.env.PATH}` },
        encoding: "utf8",
        timeout: 10_000,
      },
    );
    assert.equal(result.status, 0, result.stderr);
    assert.equal(readFileSync(join(root, "layer.tar"), "utf8"), "artifact");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
