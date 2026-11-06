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
export interface ToolCheckpoint {
  offset: number;
  operation: string;
}

export interface EvidenceItem {
  label: string;
  detail: string;
  source?: string;
  level?: "critical" | "serious" | "moderate" | "uncertain";
}

export interface EvidenceBlock {
  kind: "timeline" | "risk" | "reasoning";
  title: string;
  items: EvidenceItem[];
  outcomes?: EvidenceItem[];
}

export type MessageBlock =
  | { kind: "markdown"; html: string }
  | { kind: "chart"; chart: ChartSpec }
  | { kind: "checkpoint"; operations: string[] }
  | EvidenceBlock;

function literal(value: unknown, limit: number): value is string {
  return (
    typeof value === "string" &&
    value.length <= limit &&
    value.trim().length > 0
  );
}

function riskLevel(
  value: unknown,
): value is NonNullable<EvidenceItem["level"]> {
  return (
    value === "critical" ||
    value === "serious" ||
    value === "moderate" ||
    value === "uncertain"
  );
}

function evidenceItems(
  value: unknown,
  risk: boolean,
  limit: number,
): EvidenceItem[] | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > limit)
    return null;
  const items: EvidenceItem[] = [];
  for (const raw of value as unknown[]) {
    if (typeof raw !== "object" || raw === null) return null;
    const item = raw as Record<string, unknown>;
    if (
      Object.keys(item).some(
        (key) => !["label", "detail", "source", "level"].includes(key),
      ) ||
      !literal(item.label, 160) ||
      !literal(item.detail, 600) ||
      !(item.source === undefined || literal(item.source, 180)) ||
      (risk ? !riskLevel(item.level) : item.level !== undefined)
    )
      return null;
    items.push({
      label: item.label,
      detail: item.detail,
      ...(item.source === undefined ? {} : { source: item.source }),
      ...(riskLevel(item.level) ? { level: item.level } : {}),
    });
  }
  return items;
}

function parseEvidence(
  kind: EvidenceBlock["kind"],
  content: string,
): EvidenceBlock | null {
  if (content.length > 16_384) return null;
  try {
    const payload: unknown = JSON.parse(content);
    if (typeof payload !== "object" || payload === null) return null;
    const value = payload as Record<string, unknown>;
    if (
      Object.keys(value).some(
        (key) => !["title", "items", "outcomes"].includes(key),
      ) ||
      !literal(value.title, 120)
    )
      return null;
    const items = evidenceItems(value.items, kind === "risk", 20);
    if (!items) return null;
    if (value.outcomes === undefined)
      return { kind, title: value.title, items };
    const outcomes =
      kind === "reasoning" ? evidenceItems(value.outcomes, false, 4) : null;
    return outcomes ? { kind, title: value.title, items, outcomes } : null;
  } catch {
    return null;
  }
}

interface RenderContext extends Env {
  tableLabel: string;
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

markdown.core.ruler.push("email_references", (state) => {
  for (const block of state.tokens) {
    if (!block.children) continue;
    const output: Token[] = [];
    let linked = false;
    for (const token of block.children) {
      if (token.type === "link_open") linked = true;
      if (token.type === "link_close") linked = false;
      if (linked || !["text", "code_inline"].includes(token.type)) {
        output.push(token);
        continue;
      }
      let offset = 0;
      for (const match of token.content.matchAll(/\bGDR-[A-F0-9]{32}\b/g)) {
        const start = match.index;
        const prefix = new state.Token(token.type, token.tag, 0);
        prefix.content = token.content.slice(offset, start);
        output.push(prefix);
        const open = new state.Token("link_open", "a", 1);
        open.attrSet("href", `/email/${match[0]}`);
        open.attrSet("data-email-reference", match[0]);
        const label = new state.Token(token.type, token.tag, 0);
        label.content = match[0];
        output.push(open, label, new state.Token("link_close", "a", -1));
        offset = start + match[0].length;
      }
      if (!offset) output.push(token);
      else {
        const suffix = new state.Token(token.type, token.tag, 0);
        suffix.content = token.content.slice(offset);
        output.push(suffix);
      }
    }
    block.children = output;
  }
});

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
  if (!/^\/pengolo\/files\/[a-f0-9]{32}(?:\?|$)/.test(source))
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

markdown.renderer.rules.table_open = (tokens, index, _options, context) => {
  const id = tokens[index]?.attrGet("data-table-index") ?? "0";
  const label = markdown.utils.escapeHtml(
    (context as RenderContext).tableLabel,
  );
  return `<div class="p-table"><div class="p-table-actions"><button type="button" data-table-copy="${id}">Copy table</button><button type="button" data-table-download="${id}">Download Excel</button></div><div class="p-table-scroll" tabindex="0" role="region" aria-label="${label}"><table>`;
};
markdown.renderer.rules.table_close = () => "</table></div></div>";

export function renderPengoloMarkdown(
  text: string,
  id: string,
  editable = true,
  tableLabel = "Scrollable table",
  checkpoints: readonly ToolCheckpoint[] = [],
  streaming = false,
): { blocks: MessageBlock[]; tables: string[][][]; codes: string[] } {
  const scope = `p-${id.replace(/[^a-z0-9_-]/gi, "-")}`;
  const env: RenderContext = {
    tableLabel,
    scope,
    docId: scope,
    editable,
    anchors: new Map(),
    tables: [],
    codes: [],
  };
  const tokens = markdown.parse(text, env);
  const blocks: MessageBlock[] = [];
  const lines = text.split("\n");
  const starts = [0];
  if (checkpoints.length) {
    let offset = 0;
    for (const line of lines) starts.push((offset += line.length + 1));
  }
  let next = 0;
  let from = 0;
  function flush(index: number) {
    if (index <= from) return;
    blocks.push({
      kind: "markdown",
      html: markdown.renderer.render(
        tokens.slice(from, index),
        markdown.options,
        env,
      ),
    });
    from = index;
  }
  function progress(index: number, offset: number) {
    const first = checkpoints[next];
    if (!first || next >= 32 || first.offset > offset) return;
    const operations = new Set<string>();
    while (next < 32) {
      const checkpoint = checkpoints[next];
      if (!checkpoint || checkpoint.offset > offset) break;
      operations.add(checkpoint.operation);
      next++;
    }
    flush(index);
    blocks.push({ kind: "checkpoint", operations: [...operations] });
  }
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index];
    if (!token?.map || token.level !== 0) continue;
    progress(index, starts[token.map[0]] ?? 0);
    if (token.type !== "fence") continue;
    const kind = token.info.trim();
    const close = lines[token.map[1] - 1]?.trim();
    if (!close?.startsWith(token.markup) || !/^(?:`+|~+)$/.test(close)) {
      if (streaming && ["timeline", "risk", "reasoning"].includes(kind)) {
        flush(index);
        from = index + 1;
      }
      continue;
    }
    const chart = kind === "chart" ? parseChart(token.content) : null;
    const evidence =
      kind === "timeline" || kind === "reasoning" || kind === "risk"
        ? parseEvidence(kind, token.content)
        : null;
    if (!chart && !evidence) continue;
    flush(index);
    if (chart) blocks.push({ kind: "chart", chart });
    else if (evidence) blocks.push(evidence);
    from = index + 1;
  }
  flush(tokens.length);
  progress(tokens.length, text.length);
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
