import { randomUUID } from "node:crypto";
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { pathToFileURL } from "node:url";
import {
  decodedMappings,
  encodedMappings,
  TraceMap,
  type SourceMapSegment,
} from "@jridgewell/trace-mapping";
import {
  createCoverageMap,
  type CoverageMapData,
} from "@vitest/istanbul-lib-coverage";
import { create, createContext } from "@vitest/istanbul-lib-report";
import { parse as parseComponent } from "@vue/compiler-sfc";
import { parse } from "acorn";
import { convert } from "ast-v8-to-istanbul";
import type { Page } from "@playwright/test";

interface Source {
  path: string;
  content: string;
}

interface SourceMap {
  version: 3;
  sources: string[];
  sourcesContent: (string | null)[];
  names: string[];
  mappings: string;
}

type BrowserProfile = Awaited<ReturnType<Page["coverage"]["stopJSCoverage"]>>;

/** Confirm a logical map path against unique, byte-identical Bazel source inputs. */
export function resolveSource(
  reference: string,
  content: string,
  candidates: readonly Source[],
): string | undefined {
  const clean = reference
    .split("?", 1)[0]
    ?.replaceAll("\\", "/")
    .replace(/^(?:\.\.?\/)+/, "");
  if (!clean) return undefined;
  const matching = candidates.filter(
    (source) =>
      source.content === content &&
      (source.path === clean ||
        source.path.endsWith(`/${clean}`) ||
        clean.endsWith(`/${source.path}`)),
  );
  return matching.length === 1 ? matching[0]?.path : undefined;
}

function sourceMap(raw: unknown): SourceMap {
  if (
    raw === null ||
    typeof raw !== "object" ||
    !("version" in raw) ||
    raw.version !== 3 ||
    !("mappings" in raw) ||
    typeof raw.mappings !== "string" ||
    !("sources" in raw) ||
    !isList(raw.sources, isString) ||
    !("sourcesContent" in raw) ||
    !isList(raw.sourcesContent, isContent) ||
    raw.sourcesContent.length !== raw.sources.length ||
    !("names" in raw) ||
    !isList(raw.names, isString)
  ) {
    throw new Error("Browser coverage needs a complete source map");
  }
  return {
    version: 3,
    mappings: raw.mappings,
    names: raw.names,
    sources: raw.sources,
    sourcesContent: raw.sourcesContent,
  };
}

function isList<T>(
  value: unknown,
  accept: (item: unknown) => item is T,
): value is T[] {
  return Array.isArray(value) && value.every(accept);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isContent(value: unknown): value is string | null {
  return value === null || isString(value);
}

function repositorySources(root: string): Source[] {
  const sources: Source[] = [];
  const pending = [join(root, "ui"), join(root, "dashboard")].filter((path) =>
    existsSync(path),
  );
  for (const directory of pending) {
    // rules_js resolves symlink Dirents relative to the non-recursive read root.
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      if (["node_modules", ".nuxt", ".output", ".git"].includes(entry.name))
        continue;

      const full = join(directory, entry.name);
      if (entry.isDirectory()) {
        pending.push(full);
      } else if (
        /\.(?:[cm]?ts|tsx|vue)$/.test(entry.name) &&
        !entry.name.endsWith(".d.ts") &&
        (entry.isFile() || entry.isSymbolicLink())
      ) {
        sources.push({
          path: `front/${relative(root, full)}`,
          content: readFileSync(full, "utf8"),
        });
      }
    }
  }
  return sources;
}

function sourceMapReference(source: string): string | undefined {
  return Array.from(source.matchAll(/^\/\/[#@]\s*sourceMappingURL=(\S+)/gm)).at(
    -1,
  )?.[1];
}

function inlineMap(reference: string): unknown {
  const separator = reference.indexOf(",");
  if (separator < 0) throw new Error("Invalid inline browser source map");
  const payload = reference.slice(separator + 1);
  const text = reference.slice(0, separator).endsWith(";base64")
    ? Buffer.from(payload, "base64").toString("utf8")
    : decodeURIComponent(payload);
  return JSON.parse(text);
}

async function loadMap(
  page: Page,
  scriptURL: URL,
  reference: string,
): Promise<unknown> {
  if (reference.startsWith("data:application/json"))
    return inlineMap(reference);
  const url = new URL(reference, scriptURL);
  if (url.origin !== scriptURL.origin)
    throw new Error("Cross-origin source map in a first-party browser test");
  const response = await page.request.get(url.href);
  if (!response.ok())
    throw new Error(`Browser source map returned HTTP ${response.status()}`);
  return JSON.parse(await response.text());
}

interface Script {
  code: string;
  url: string;
  functions: BrowserProfile[number]["functions"];
}

type Measurement =
  | {
      status: "measured";
      data: CoverageMapData;
      unmappedTemplateSegments: number;
    }
  | { status: "missing"; reason: string }
  | { status: "external" };

function splitLines(text: string): string[] {
  return text.split(/\r\n|[\n\r\u2028\u2029]/);
}

function checkMappings(
  map: SourceMap,
  code: string,
  verified: ReadonlySet<string>,
): number {
  if (!map.mappings || !/^[A-Za-z0-9+/,;]+$/.test(map.mappings))
    throw new Error("Browser source map has no valid mappings");

  const generated = splitLines(code);
  const originals = map.sourcesContent.map((content) =>
    content === null ? null : splitLines(content),
  );
  const vueLocations = new Map<
    number,
    ReturnType<typeof parseComponent>["descriptor"]
  >();
  let unmapped = 0;
  const mappings = decodedMappings(new TraceMap(map)).map((segments, index) =>
    segments.map((segment): SourceMapSegment => {
      const [column, source, line, offset, name] = segment;
      const row = generated[index];
      if (row === undefined || column < 0 || column > row.length)
        throw new Error("Browser source map points outside generated code");
      if (source === undefined) return segment;
      if (line === undefined || offset === undefined)
        throw new Error("Browser source map has an incomplete segment");

      const original = originals[source];
      if (
        original === undefined ||
        (name !== undefined && map.names[name] === undefined)
      )
        throw new Error(
          "Browser source map references an unknown source or name",
        );
      const path = map.sources[source];
      const content = map.sourcesContent[source];
      if (original === null || path === undefined || !verified.has(path))
        return segment;

      const text = original[line];
      if (text === undefined || offset < 0)
        throw new Error("Browser source map points outside original code");
      if (offset <= text.length) return segment;

      if (path.endsWith(".vue") && typeof content === "string") {
        let descriptor = vueLocations.get(source);
        if (!descriptor) {
          const parsed = parseComponent(content, { filename: path });
          if (parsed.errors.length)
            throw new Error("Invalid Vue source in browser map");
          descriptor = parsed.descriptor;
          vueLocations.set(source, descriptor);
        }

        // Vue can emit out-of-line columns for multiline template expressions.
        // An unmapped segment retains the surrounding valid script mappings.
        const position = line + 1;
        const template = descriptor.template?.loc;
        const inScript = [descriptor.script, descriptor.scriptSetup].some(
          (script) =>
            script &&
            script.loc.start.line <= position &&
            position <= script.loc.end.line,
        );
        if (
          template &&
          template.start.line <= position &&
          position <= template.end.line &&
          !inScript
        ) {
          unmapped++;
          return [column];
        }
      }
      throw new Error("Browser source map points outside original code");
    }),
  );
  if (unmapped)
    map.mappings = encodedMappings(new TraceMap({ ...map, mappings }));
  return unmapped;
}

/** Measure transformed code only after establishing its original Bazel source identity. */
export async function measureScript(
  script: Script,
  rawMap: unknown,
  sourceInputs: readonly Source[],
  workspace: string,
): Promise<Measurement> {
  try {
    const map = sourceMap(rawMap);
    const verified = new Set<string>();
    const unresolved: string[] = [];
    map.sources = map.sources.map((name, offset) => {
      const content = map.sourcesContent[offset];
      const canonical =
        content == null
          ? undefined
          : resolveSource(name, content, sourceInputs);
      if (canonical) {
        const path = join(workspace, canonical);
        verified.add(path);
        return path;
      }
      if (
        !name.includes("/node_modules/") &&
        /\.(?:[cm]?ts|tsx|vue)(?:\?|$)/.test(name)
      )
        unresolved.push(name);
      return name;
    });
    if (verified.size === 0)
      return unresolved.length
        ? {
            status: "missing",
            reason: "Source bytes or paths differ from Bazel inputs",
          }
        : { status: "external" };

    const unmappedTemplateSegments = checkMappings(map, script.code, verified);
    const ast = parse(script.code, {
      ecmaVersion: "latest",
      sourceType: "module",
      locations: true,
      ranges: true,
    });
    const data = await convert<typeof ast, typeof ast>({
      ast,
      code: script.code,
      sourceMap: map,
      coverage: {
        url: pathToFileURL(join(workspace, new URL(script.url).pathname)).href,
        functions: script.functions,
      },
    });
    const trusted = Object.fromEntries(
      Object.entries(data).filter(([path]) => verified.has(path)),
    );
    if (
      !Object.values(trusted).some(
        (file) =>
          Object.keys(file.statementMap).length ||
          Object.keys(file.fnMap).length,
      )
    )
      return { status: "missing", reason: "No executable points remapped" };

    return { status: "measured", data: trusted, unmappedTemplateSegments };
  } catch (error: unknown) {
    return {
      status: "missing",
      reason:
        error instanceof Error
          ? error.message
          : "Invalid browser instrumentation",
    };
  }
}

/** Use the Istanbul reporter for the LCOV format consumed by Bazel. */
export function writeLcov(
  directory: string,
  file: string,
  workspace: string,
  reports: readonly CoverageMapData[],
): void {
  const coverageMap = createCoverageMap();
  for (const report of reports) coverageMap.merge(report);
  const context = createContext({ dir: directory, coverageMap });
  create("lcovonly", { file, projectRoot: workspace }).execute(context);
}

/** Record reliable Chromium measurements and preserve instrumentation diagnostics. */
export async function writeBrowserProfile(
  page: Page,
  profile: BrowserProfile,
  origin: string,
  workspace: string,
  stagedFront: string,
  directory: string,
  assets: string | undefined,
  diagnostics: string,
): Promise<void> {
  const sourceInputs = repositorySources(stagedFront);
  const reports: CoverageMapData[] = [];
  const failures: { script: string; reason: string }[] = [];
  const observed = new Set<string>();
  const collect = async (script: Script, readMap: () => unknown) => {
    try {
      const measurement = await measureScript(
        script,
        await readMap(),
        sourceInputs,
        workspace,
      );
      if (measurement.status === "measured") {
        reports.push(measurement.data);
        if (measurement.unmappedTemplateSegments)
          failures.push({
            script: script.url,
            reason: `${measurement.unmappedTemplateSegments} invalid Vue template map segments left unmeasured`,
          });
      }
      if (measurement.status === "missing")
        failures.push({ script: script.url, reason: measurement.reason });
    } catch (error: unknown) {
      failures.push({ script: script.url, reason: String(error) });
    }
  };

  for (const script of profile) {
    if (!script.url.startsWith(`${origin}/`)) continue;
    const url = new URL(script.url);
    observed.add(url.pathname);
    if (!script.source || !script.functions.length) {
      failures.push({
        script: url.pathname,
        reason: "No Chromium profile data",
      });
      continue;
    }
    const reference = sourceMapReference(script.source);
    if (!reference) {
      failures.push({ script: url.pathname, reason: "No browser source map" });
      continue;
    }
    await collect(
      { code: script.source, url: script.url, functions: script.functions },
      () => loadMap(page, url, reference),
    );
  }

  // Empty V8 ranges establish zero only for actual emitted code from a profiled build.
  if (assets && reports.length && existsSync(assets)) {
    for (const file of readdirSync(assets).sort()) {
      if (!file.endsWith(".js")) continue;
      const path = `/_nuxt/${file}`;
      if (observed.has(path)) continue;
      const code = readFileSync(join(assets, file), "utf8");
      const reference = sourceMapReference(code);
      await collect(
        { code, url: new URL(path, origin).href, functions: [] },
        () => {
          if (!reference) throw new Error("No source map for emitted chunk");
          if (reference.startsWith("data:application/json"))
            return inlineMap(reference);
          if (reference !== `${file}.map`)
            throw new Error("Unexpected emitted source map path");
          return JSON.parse(readFileSync(join(assets, reference), "utf8"));
        },
      );
    }
  }

  if (!reports.length)
    failures.push({
      script: origin,
      reason: "No first-party Chromium coverage measurements",
    });

  const id = randomUUID();
  if (failures.length)
    writeFileSync(
      join(diagnostics, `browser-coverage-${id}.json`),
      JSON.stringify(failures),
    );
  if (reports.length)
    writeLcov(directory, `browser-${id}.dat`, workspace, reports);
}
