<script setup lang="ts">
import createDOMPurify from "dompurify";
import MarkdownIt from "markdown-it";
import { renderEmailSignature, signatureImageSource } from "~/utils/email";
const props = defineProps<{ html: string }>();
const { t } = useI18n();
const imagesShown = ref(false);
watch(
  () => props.html,
  () => {
    imagesShown.value = false;
  },
);
onDeactivated(() => {
  imagesShown.value = false;
});
const markdown = new MarkdownIt("commonmark", { html: true, breaks: true });
markdown.renderer.rules.hardbreak = (tokens, index) => {
  const previous = index > 0 ? tokens.at(index - 1) : undefined;
  return previous?.type === "html_inline" &&
    /<br\s*\/?>\s*$/i.test(previous.content)
    ? ""
    : "<br>";
};
markdown.renderer.rules.softbreak = markdown.renderer.rules.hardbreak;
const sanitizer = import.meta.client ? createDOMPurify(window) : null;
let imageCount = 0;
sanitizer?.addHook("afterSanitizeAttributes", (node) => {
  if (node.nodeName !== "IMG") return;
  const source = signatureImageSource(node.getAttribute("src") ?? "");
  if (!source) {
    node.remove();
    return;
  }
  imageCount++;
  const alt = (node.getAttribute("alt") ?? "").slice(0, 512);
  if (!imagesShown.value) {
    const placeholder = node.ownerDocument.createElement("span");
    placeholder.textContent = alt || t("email.signatureImage");
    node.replaceWith(placeholder);
    return;
  }
  node.setAttribute("src", source);
  node.setAttribute("alt", alt);
  node.setAttribute("referrerpolicy", "no-referrer");
  node.setAttribute("style", "max-width:100%;height:auto;border:0");
  for (const name of ["width", "height"]) {
    const value = node.getAttribute(name) ?? "";
    if (!/^\d{1,4}$/.test(value) || Number(value) < 1 || Number(value) > 1024)
      node.removeAttribute(name);
  }
});
const preview = computed(() => {
  imageCount = 0;
  const rendered = markdown.render(props.html.replace(/\r\n?/g, "\n"));
  const expanded = renderEmailSignature(rendered, {
    ref: "GDR-0123456789ABCDEF0123456789ABCDEF",
    subject: t("email.signatureExampleSubject"),
    sender: "sender@example.org",
    recipient: "recipient@example.org",
    date: new Date().toISOString().slice(0, 10),
  });
  const safe =
    sanitizer?.sanitize(expanded, {
      ALLOWED_TAGS: [
        "p",
        "br",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "div",
        "span",
        "a",
        "ul",
        "ol",
        "li",
        "code",
        "pre",
        "small",
        "img",
        "blockquote",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
      ],
      ALLOWED_ATTR: ["href", "src", "alt", "width", "height", "start"],
      ALLOW_DATA_ATTR: false,
    }) ?? "";
  const content = safe
    .replace(/(<\/(?:p|div|ul|ol|li|pre|blockquote|h[1-6])>)\n/g, "$1")
    .replace(/(<(?:ul|ol)>)\n/g, "$1");
  const imagePolicy = imagesShown.value ? "https:" : "&#39;none&#39;";
  return {
    hasImages: imageCount > 0,
    document:
      '<!doctype html><html lang="en"><head><meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; img-src ' +
      imagePolicy +
      '"><style>body{font:14px system-ui;color:#17212f;margin:12px;overflow-wrap:anywhere;white-space:pre-line}pre{white-space:pre-wrap}img{max-width:100%;height:auto}</style></head><body>' +
      content +
      "</body></html>",
  };
});
</script>
<template>
  <div class="space-y-2">
    <iframe
      :title="$t('email.signaturePreview')"
      :srcdoc="preview.document"
      sandbox=""
      referrerpolicy="no-referrer"
      class="h-44 w-full rounded-control border border-border bg-surface"
    />
    <UiButton
      v-if="preview.hasImages && !imagesShown"
      variant="ghost"
      size="sm"
      @click="imagesShown = true"
      >{{ $t("email.signatureShowImages") }}</UiButton
    >
  </div>
</template>
