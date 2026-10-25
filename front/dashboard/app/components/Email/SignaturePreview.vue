<script setup lang="ts">
import DOMPurify from "dompurify";
import { renderEmailSignature } from "~/utils/email";
const props = defineProps<{ html: string }>();
const { t } = useI18n();
const document = computed(() => {
  const expanded = renderEmailSignature(props.html, {
    ref: "GDR-0123456789ABCDEF0123456789ABCDEF",
    subject: t("email.signatureExampleSubject"),
    sender: "sender@example.org",
    recipient: "recipient@example.org",
    date: new Date().toISOString().slice(0, 10),
  });
  const safe = DOMPurify.sanitize(expanded, {
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
    ],
    ALLOWED_ATTR: ["href"],
    ALLOW_DATA_ATTR: false,
  });
  return (
    '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;"><style>body{font:14px system-ui;color:#17212f;margin:12px;overflow-wrap:anywhere}</style></head><body>' +
    safe +
    "</body></html>"
  );
});
</script>
<template>
  <iframe
    :title="$t('email.signaturePreview')"
    :srcdoc="document"
    sandbox=""
    referrerpolicy="no-referrer"
    class="h-36 w-full rounded-control border border-border bg-surface"
  />
</template>
