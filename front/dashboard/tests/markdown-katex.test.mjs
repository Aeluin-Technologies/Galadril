import assert from "node:assert/strict";
import test from "node:test";

import MarkdownIt from "markdown-it";
import markdownItKatex from "@traptitech/markdown-it-katex";

test("KaTeX errors escape attacker-controlled markup", () => {
  const markdown = new MarkdownIt({ html: false }).use(markdownItKatex);
  const rendered = markdown.render("$\\notacommand{<img src=x onerror=alert(1)>}$");

  assert.doesNotMatch(rendered, /<img\b/i);
});
