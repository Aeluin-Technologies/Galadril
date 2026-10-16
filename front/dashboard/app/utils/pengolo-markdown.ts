import MarkdownIt, { type Token, type Env } from "markdown-it";
import markdownItKatex from "@traptitech/markdown-it-katex";
import footnote from "markdown-it-footnote";
import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import rust from "highlight.js/lib/languages/rust";
import bash from "highlight.js/lib/languages/bash";
import json from "highlight.js/lib/languages/json";
import sql from "highlight.js/lib/languages/sql";
import yaml from "highlight.js/lib/languages/yaml";
import xml from "highlight.js/lib/languages/xml";
import css from "highlight.js/lib/languages/css";

export interface ChartSpec {
  type: "bar" | "line" | "pie" | "donut";
  title: string;
  labels: string[];
  series: { name: string; values: number[]; color?: string }[];
}
export type MessageBlock =
  { kind: "markdown"; html: string } | { kind: "chart"; chart: ChartSpec };

interface RenderContext extends Env {
  scope: string;
  docId: string;
  editable: boolean;
  anchors: Map<string, string>;
  tables: string[][][];
  codes: string[];
}

for (const [name, language] of Object.entries({
  python,
  javascript,
  typescript,
  rust,
  bash,
  json,
  sql,
  yaml,
  xml,
  css,
}))
  hljs.registerLanguage(name, language);

const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
  .use(markdownItKatex)
  .use(footnote);

function slug(value: string): string {
  return (
    value
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^\p{L}\p{N}_\s-]/gu, "")
      .trim()
      .replace(/[\s-]+/g, "-") || "section"
  );
}

function plain(token?: Token): string {
  return (
    token?.children
      ?.map((child) =>
        child.type === "text" ||
        child.type === "code_inline" ||
        child.type === "math_inline"
          ? child.content
          : "",
      )
      .join("") ??
    token?.content ??
    ""
  );
}

function task(tokens: readonly Token[], index: number): boolean {
  return (
    tokens[index]?.type === "inline" &&
    tokens[index - 1]?.type === "paragraph_open" &&
    tokens[index - 2]?.type === "list_item_open" &&
    /^\[[ xX]\]\s/.test(tokens[index]?.children?.[0]?.content ?? "")
  );
}

markdown.core.ruler.push("pengolo_identifiers", (state) => {
  const env = state.env as unknown as RenderContext;
  const used = new Set<string>();
  let taskIndex = 0;
  let table: string[][] | null = null;
  let row: string[] | null = null;
  for (let index = 0; index < state.tokens.length; index++) {
    const token = state.tokens[index];
    if (!token) continue;
    if (token.type === "heading_open") {
      const title = plain(state.tokens[index + 1]);
      const base = slug(title);
      let suffix = 0;
      let anchor = base;
      while (used.has(anchor)) anchor = `${base}-${++suffix}`;
      used.add(anchor);
      const id = `${env.scope}-${anchor}`;
      token.attrSet("id", id);
      env.anchors.set(anchor, id);
      const unnumbered = slug(title.replace(/^\d+[\s.)-]+/, ""));
      if (!env.anchors.has(unnumbered)) env.anchors.set(unnumbered, id);
    }
    if (task(state.tokens, index)) {
      const first = token.children?.[0];
      if (first && token.children) {
        const checkbox = new state.Token("pengolo_checkbox", "input", 0);
        checkbox.attrSet("data-task-index", String(taskIndex++));
        if (/^\[[xX]\]/.test(first.content))
          checkbox.attrSet("checked", "checked");
        first.content = first.content.slice(4);
        token.children.unshift(checkbox);
      }
    }
    if (token.type === "table_open") {
      token.attrSet("data-table-index", String(env.tables.length));
      table = [];
      env.tables.push(table);
    } else if (token.type === "tr_open" && table) {
      row = [];
      table.push(row);
    } else if (token.type === "inline" && row && table) row.push(plain(token));
    else if (token.type === "tr_close") row = null;
    else if (token.type === "table_close") table = null;
  }
});

markdown.renderer.rules.pengolo_checkbox = (
  tokens,
  index,
  _options,
  context,
) => {
  const env = context as unknown as RenderContext;
  const token = tokens[index];
  const id = token?.attrGet("data-task-index") ?? "0";
  return `<input type="checkbox" data-task-index="${id}" aria-label="Task ${Number(id) + 1}"${token?.attrGet("checked") ? " checked" : ""}${env.editable ? "" : " disabled"}> `;
};

const link = markdown.renderer.rules.link_open;
markdown.renderer.rules.link_open = (
  tokens,
  index,
  options,
  context,
  renderer,
) => {
  const token = tokens[index];
  const env = context as unknown as RenderContext;
  const href = String(token?.attrGet("href") ?? "");
  if (/^https?:\/\//i.test(href)) {
    token?.attrSet("target", "_blank");
    token?.attrSet("rel", "noopener noreferrer");
    token?.attrSet("referrerpolicy", "no-referrer");
  } else if (href.startsWith("#")) {
    let fragment = href.slice(1);
    try {
      fragment = decodeURIComponent(fragment);
    } catch {
      /* Malformed fragments stay local. */
    }
    token?.attrSet(
      "href",
      "#" +
        (env.anchors.get(slug(fragment)) ?? `${env.scope}-${slug(fragment)}`),
    );
  }
  return link
    ? link(tokens, index, options, context, renderer)
    : renderer.renderToken(tokens, index, options);
};

markdown.renderer.rules.image = (
  tokens,
  index,
  options,
  _context,
  renderer,
) => {
  const token = tokens[index];
  if (!token) return "";
  const source = String(token.attrGet("src") ?? "");
  if (/^https:\/\//i.test(source))
    token.attrSet("src", `/media/image?url=${encodeURIComponent(source)}`);
  else if (!/^\/pengolo\/files\/[a-f0-9]{32}(?:\?|$)/.test(source))
    return markdown.utils.escapeHtml(token.content);
  token.attrSet("alt", token.content);
  token.attrSet("loading", "lazy");
  token.attrSet("decoding", "async");
  token.attrSet("referrerpolicy", "no-referrer");
  return renderer.renderToken(tokens, index, options);
};

markdown.renderer.rules.fence = (tokens, index, _options, context) => {
  const token = tokens[index];
  if (!token) return "";
  const env = context as unknown as RenderContext;
  const language = token.info.trim().split(/\s+/)[0]?.toLowerCase() ?? "";
  const code =
    language && hljs.getLanguage(language)
      ? hljs.highlight(token.content, { language, ignoreIllegals: true }).value
      : markdown.utils.escapeHtml(token.content);
  const codeIndex = env.codes.push(token.content) - 1;
  return `<div class="p-code"><div class="p-code-heading"><span>${markdown.utils.escapeHtml(language || "text")}</span><button type="button" data-code-copy="${codeIndex}">Copy code</button></div><pre><code class="hljs">${code}</code></pre></div>`;
};

markdown.renderer.rules.table_open = (tokens, index) => {
  const id = tokens[index]?.attrGet("data-table-index") ?? "0";
  return `<div class="p-table"><div class="p-table-actions"><button type="button" data-table-copy="${id}">Copy table</button><button type="button" data-table-download="${id}">Download Excel</button></div><table>`;
};
markdown.renderer.rules.table_close = () => "</table></div>";

export function renderPengoloMarkdown(
  text: string,
  id: string,
  editable = true,
): { blocks: MessageBlock[]; tables: string[][][]; codes: string[] } {
  const scope = `p-${id.replace(/[^a-z0-9_-]/gi, "-")}`;
  const env: RenderContext = {
    scope,
    docId: scope,
    editable,
    anchors: new Map(),
    tables: [],
    codes: [],
  };
  const tokens = markdown.parse(text, env);
  const blocks: MessageBlock[] = [];
  let from = 0;
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index];
    if (token?.type !== "fence" || token.info.trim() !== "chart") continue;
    const chart = parseChart(token.content);
    if (!chart) continue;
    if (index > from)
      blocks.push({
        kind: "markdown",
        html: markdown.renderer.render(
          tokens.slice(from, index),
          markdown.options,
          env,
        ),
      });
    blocks.push({ kind: "chart", chart });
    from = index + 1;
  }
  if (from < tokens.length)
    blocks.push({
      kind: "markdown",
      html: markdown.renderer.render(tokens.slice(from), markdown.options, env),
    });
  return { blocks, tables: env.tables, codes: env.codes };
}

export function markdownWithTaskChecks(
  text: string,
  checks: Record<string, boolean>,
): string {
  const tokens = new MarkdownIt({ html: false }).parse(text, {});
  const lines = text.split("\n");
  let taskIndex = 0;
  for (let index = 0; index < tokens.length; index++) {
    if (!task(tokens, index)) continue;
    const checked = checks[String(taskIndex++)];
    const line = tokens[index]?.map?.[0];
    if (checked === undefined || line === undefined) continue;
    const original = lines[line];
    if (original !== undefined)
      lines[line] = original.replace(
        /^(\s*(?:>\s*)*(?:[-+*]|\d+[.)])\s+)\[[ xX]\]/,
        `$1[${checked ? "x" : " "}]`,
      );
  }
  return lines.join("\n");
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function parseChart(text: string): ChartSpec | null {
  if (text.length > 32768) return null;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (
    !object(value) ||
    Object.keys(value).some(
      (key) => !["type", "title", "labels", "series"].includes(key),
    )
  )
    return null;
  if (!["bar", "line", "pie", "donut"].includes(String(value.type)))
    return null;
  if (
    value.title !== undefined &&
    (typeof value.title !== "string" || value.title.length > 256)
  )
    return null;
  if (
    !Array.isArray(value.labels) ||
    !value.labels.length ||
    value.labels.length > 100 ||
    !value.labels.every(
      (label) => typeof label === "string" && label.length <= 128,
    )
  )
    return null;
  if (
    !Array.isArray(value.series) ||
    !value.series.length ||
    value.series.length > 8
  )
    return null;
  for (const series of value.series) {
    if (
      !object(series) ||
      Object.keys(series).some(
        (key) => !["name", "values", "color"].includes(key),
      )
    )
      return null;
    if (
      typeof series.name !== "string" ||
      series.name.length > 128 ||
      !Array.isArray(series.values) ||
      series.values.length !== value.labels.length
    )
      return null;
    if (
      !series.values.every(
        (number) =>
          typeof number === "number" &&
          Number.isFinite(number) &&
          Math.abs(number) <= 1e15,
      )
    )
      return null;
    if (
      series.color !== undefined &&
      (typeof series.color !== "string" ||
        !/^#[a-f0-9]{6}$/i.test(series.color))
    )
      return null;
  }
  return {
    type: value.type as ChartSpec["type"],
    title: (value.title as string | undefined) ?? "",
    labels: value.labels as string[],
    series: value.series as ChartSpec["series"],
  };
}
