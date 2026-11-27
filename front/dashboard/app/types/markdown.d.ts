declare module "@traptitech/markdown-it-katex" {
  import type MarkdownIt from "markdown-it";
  const plugin: (markdown: MarkdownIt) => void;
  export default plugin;
}

declare module "markdown-it-footnote" {
  import type MarkdownIt from "markdown-it";
  const plugin: (markdown: MarkdownIt) => void;
  export default plugin;
}
