<script setup lang="ts">
import MarkdownIt from "markdown-it";
import markdownItKatex from "@traptitech/markdown-it-katex";
import DOMPurify from "dompurify";
import type { PengoloMessage } from "~/utils/pengolo-api";
const props = defineProps<{ message: PengoloMessage; streaming?: boolean }>();
const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
}).use(markdownItKatex);
const text = computed(
  () =>
    props.message.parts
      ?.flatMap((part) => (part.kind === "markdown" ? [part.text] : []))
      .join("\n\n") || props.message.text,
);
const rendered = computed(() =>
  DOMPurify.sanitize(markdown.render(text.value)),
);
const references = computed(() => props.message.evidence ?? []);
const artifacts = computed(
  () => props.message.parts?.filter((part) => part.kind === "artifact") ?? [],
);
const copied = ref(false);
async function copy() {
  try {
    await navigator.clipboard.writeText(text.value);
    copied.value = true;
  } catch {
    copied.value = false;
  }
}
</script>

<template>
  <article
    :class="['p-message', message.role]"
    :aria-label="message.role === 'user' ? 'Your message' : 'Pengolo response'"
  >
    <div class="p-message-body">
      <div v-if="message.role === 'user'" class="p-user-text">
        {{ message.text }}
      </div>
      <div v-else class="p-markdown" v-html="rendered" />
      <div v-if="message.fileIds?.length" class="p-file-chips">
        <span v-for="id in message.fileIds" :key="id" class="p-chip"
          >Document · {{ id.slice(0, 8) }}</span
        >
      </div>
      <div
        v-if="references.length"
        class="p-file-chips"
        aria-label="Evidence references"
      >
        <span
          v-for="reference in references"
          :key="`${reference.resource_type}/${reference.resource_id}`"
          class="p-chip"
          >{{ reference.resource_type.replaceAll("_", " ") }} ·
          {{ reference.resource_id }}</span
        >
      </div>
      <div v-for="artifact in artifacts" :key="artifact.id" class="p-chip">
        {{ artifact.title }}
      </div>
      <p v-if="streaming && !message.text" class="p-muted" role="status">
        Working with your authorized context<span class="p-thinking">…</span>
      </p>
      <UtilsButton
        variant="ghost"
        v-if="message.role === 'assistant' && message.text && !streaming"
        type="button"
        class="p-copy"
        @click="copy"
      >
        {{ copied ? "Copied" : "Copy response" }}
      </UtilsButton>
    </div>
  </article>
</template>
