import assert from "node:assert/strict";
import { test } from "node:test";
import {
  renderPengoloMarkdown,
  markdownWithTaskChecks,
  parseChart,
} from "../app/utils/pengolo-markdown.ts";
import { tableWorkbook, tableClipboard } from "../app/utils/pengolo-export.ts";
import ExcelJS from "exceljs";

test("structured evidence and real tool checkpoints render in response order", () => {
  const intro = "Introduction\n\n";
  const fence = (kind: string, body: unknown) =>
    "\x60\x60\x60" + kind + "\n" + JSON.stringify(body) + "\n\x60\x60\x60\n\n";
  const text =
    intro +
    fence("timeline", {
      title: "Chronology",
      items: [
        {
          label: "6 November",
          detail: "Publication",
          source: "Exhibit 2, p. 3",
        },
      ],
    }) +
    fence("risk", {
      title: "Risk",
      items: [{ label: "Notice", detail: "Date disputed", level: "uncertain" }],
    }) +
    fence("reasoning", {
      title: "Reasoning",
      items: [{ label: "Rule", detail: "Verify the applicable version" }],
      outcomes: [{ label: "If established", detail: "Review the consequence" }],
    });
  const result = renderPengoloMarkdown(text, "report", false, "Table", [
    { offset: intro.length, operation: "web" },
  ]);
  assert.deepEqual(
    result.blocks.map((block) => block.kind),
    ["markdown", "checkpoint", "timeline", "risk", "reasoning"],
  );
  const reasoning = result.blocks.find((block) => block.kind === "reasoning");
  assert.equal(reasoning?.outcomes?.[0]?.label, "If established");
  const injected = renderPengoloMarkdown(
    fence("risk", {
      title: "Injected",
      items: [
        {
          label: "Notice",
          detail: "Test",
          level: "critical",
          href: "javascript:alert(1)",
        },
      ],
    }),
    "unsafe",
  );
  assert.equal(injected.blocks[0]?.kind, "markdown");
});

test("mail references in prose and inline code open authorized correspondence", () => {
  const ref = "GDR-0123456789ABCDEF0123456789ABCDEF";
  for (const text of [ref, `\`${ref}\``]) {
    const result = renderPengoloMarkdown(text, "message", false, "Table");
    assert.match(JSON.stringify(result.blocks), /data-email-reference/);
  }
});

test("Excel export stores model cells as literal strings and clipboard neutralizes formulas", async () => {
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.load(
    (
      await tableWorkbook([
        ["Name", "Value"],
        ["=CMD()", "42"],
      ])
    ).buffer,
  );
  assert.equal(workbook.worksheets[0]?.getCell("A2").value, "=CMD()");
  assert.equal(workbook.worksheets[0]?.getCell("B2").value, "42");
  assert.match(tableClipboard([["=CMD()", "safe"]]), /^'=CMD\(\)\tsafe$/);
});

test("headings and footnotes stay unique across messages and duplicate titles", () => {
  const source =
    "[Go](#résumé) [Second](#résumé-1)\n\n## Résumé\n\n## Résumé\n\nNote[^one].\n\n[^one]: Evidence.";
  const first = renderPengoloMarkdown(source, "first");
  const second = renderPengoloMarkdown(source, "second");
  const html = first.blocks
    .map((block) => (block.kind === "markdown" ? block.html : ""))
    .join("");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(ids.length, new Set(ids).size);
  assert.match(html, /href="#p-first-resume"/);
  assert.match(html, /href="#p-first-resume-1"/);
  assert.match(html, /footnote/);
  assert.doesNotMatch(
    second.blocks
      .map((block) => (block.kind === "markdown" ? block.html : ""))
      .join(""),
    /id="p-first-/,
  );
});

test("external links open safely and images never contact their remote source", () => {
  const { blocks } = renderPengoloMarkdown(
    "[Source](https://example.com/page) ![Photo](https://images.example/photo.png)",
    "m",
  );
  const html = blocks
    .map((block) => (block.kind === "markdown" ? block.html : ""))
    .join("");
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /Photo/);
  assert.doesNotMatch(html, /<img|\/media\/image/);
});

test("task selection preserves fenced code and only changes real task items", () => {
  const source = "- [ ] First\n- [x] Second\n\n```md\n- [ ] Example\n```";
  const saved = markdownWithTaskChecks(source, { "0": true, "1": false });
  assert.match(saved, /^- \[x\] First\n- \[ \] Second/);
  assert.match(saved, /```md\n- \[ \] Example\n```/);
  const html = renderPengoloMarkdown(saved, "m")
    .blocks.map((block) => (block.kind === "markdown" ? block.html : ""))
    .join("");
  assert.equal([...html.matchAll(/type="checkbox"/g)].length, 2);
  assert.match(html, /data-task-index="0"/);
});

test("code is highlighted, math has one visual output, and table cells stay literal", () => {
  const result = renderPengoloMarkdown(
    "```python\ndef answer():\n    return 42\n```\n\n$E=mc^2$\n\n| Label | Value |\n| --- | --- |\n| =CMD() | 42 |",
    "m",
  );
  const html = result.blocks
    .map((block) => (block.kind === "markdown" ? block.html : ""))
    .join("");
  assert.match(html, /hljs-keyword/);
  assert.match(html, /katex-html/);
  assert.match(html, /data-table-download="0"/);
  assert.deepEqual(result.tables, [
    [
      ["Label", "Value"],
      ["=CMD()", "42"],
    ],
  ]);
});

test("charts accept bounded data and reject executable or invalid options", () => {
  assert.equal(
    parseChart(
      '{"type":"donut","title":"Split","labels":["A","B"],"series":[{"name":"Share","values":[40,60],"color":"#d97706"}]}',
    )?.type,
    "donut",
  );
  assert.equal(
    parseChart(
      '{"type":"bar","labels":["A"],"series":[{"name":"S","values":[1]}],"formatter":"alert(1)"}',
    ),
    null,
  );
  assert.equal(
    parseChart(
      '{"type":"bar","labels":["A"],"series":[{"name":"S","values":[1],"color":"url(https://tracker)"}]}',
    ),
    null,
  );
});

test("wide tables have a keyboard-accessible scroll region without changing cell contents", () => {
  const result = renderPengoloMarkdown(
    "| Date | Horaire | Durée | Épreuve | Lieu | Place | Vérification au regard de la charte |\n" +
      "| --- | --- | --- | --- | --- | --- | --- |\n" +
      "| 05/11/2026 | 08:00–09:30 | 1 h 30 | Croissance | Bâtiment A — Carnot-Ravinelle | 109 | Conforme sur les horaires visibles |",
    "calendar",
  );
  const html = result.blocks
    .map((block) => (block.kind === "markdown" ? block.html : ""))
    .join("");
  assert.match(
    html,
    /class="p-table-scroll"[^>]*tabindex="0"[^>]*role="region"[^>]*aria-label="[^"]+"/,
  );
  assert.match(html, /<td>05\/11\/2026<\/td>/);
  assert.match(html, /<td>08:00–09:30<\/td>/);
  assert.equal(result.tables[0]?.[1]?.length, 7);
});
