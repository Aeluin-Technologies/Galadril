<script setup>
import MarkdownIt from "markdown-it";
import markdownItKatex from "markdown-it-katex";
import DOMPurify from "dompurify";
import {
  DocumentDuplicateIcon,
  PencilIcon,
  DocumentTextIcon,
  ChevronUpIcon,
  ChevronDownIcon,
} from "@heroicons/vue/24/outline";

const props = defineProps({
  msg: { type: Object, required: true },
  isStreaming: { type: Boolean, default: false },
});

const emit = defineEmits(["update:text", "preview-file"]);

const expandedMessages = ref({});
const editingMessageId = ref(null);
const editText = ref("");

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
});
md.use(markdownItKatex);

const preprocessMarkdown = (text) => {
  if (!text) return "";

  const lines = text.split("\n");
  const processedLines = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.includes("```") && !line.startsWith("```")) {
      const parts = line.split("```");
      processedLines.push(parts[0]);
      processedLines.push("```" + parts.slice(1).join("```"));
    } else if (
      line.endsWith("```") &&
      line.length > 3 &&
      !line.startsWith("```")
    ) {
      const codePart = line.substring(0, line.length - 3);
      processedLines.push(codePart);
      processedLines.push("```");
    } else {
      processedLines.push(line);
    }
  }

  return processedLines.join("\n");
};

const renderMarkdown = (text) => {
  if (!text) return "";
  const cleanedText = preprocessMarkdown(text);
  const rawHtml = md.render(cleanedText);
  return DOMPurify.sanitize(rawHtml);
};

const formattedText = computed(() => renderMarkdown(props.msg.text));

const toggleExpand = (msgId) => {
  expandedMessages.value[msgId] = !expandedMessages.value[msgId];
};

const isLongText = (text) => {
  if (!text) return false;
  return text.split("\n").length > 5 || text.length > 200;
};

const copyToClipboard = async (text) => {
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    console.error(err);
  }
};

const startEdit = () => {
  editingMessageId.value = props.msg.id;
  editText.value = props.msg.text;
};

const cancelEdit = () => {
  editingMessageId.value = null;
  editText.value = "";
};

const saveEdit = () => {
  emit("update:text", { id: props.msg.id, text: editText.value });
  editingMessageId.value = null;
  editText.value = "";
};
</script>

<template>
  <div class="max-w-3xl w-full mx-auto group text-sm">
    <div
      v-if="msg.role === 'user'"
      class="flex flex-col items-end max-w-[85%] ml-auto"
    >
      <div
        class="w-full bg-zinc-100 border border-zinc-200/60 rounded-2xl px-4 py-3 text-zinc-800 shadow-sm"
      >
        <div
          v-if="editingMessageId === msg.id"
          class="w-full flex flex-col items-end"
        >
          <textarea
            v-model="editText"
            class="w-full bg-transparent border-none focus:ring-0 text-sm text-zinc-800 resize-none p-0 outline-none leading-relaxed break-words"
            rows="3"
          ></textarea>
          <div class="flex items-center space-x-2 mt-3">
            <UtilsButton
              @click="cancelEdit"
              variant="secondary"
              class="!px-3 !py-1.5 rounded-xl !text-[12px]"
            >
              {{ $t("chat_component.actions.cancel") }}
            </UtilsButton>
            <UtilsButton
              @click="saveEdit"
              variant="primary"
              class="!px-3 !py-1.5 rounded-xl !text-[12px]"
            >
              {{ $t("chat_component.actions.send") }}
            </UtilsButton>
          </div>
        </div>

        <div v-else>
          <div
            v-html="formattedText"
            class="markdown-body max-w-full leading-relaxed break-words text-zinc-800"
            :class="{
              'line-clamp-[5]':
                isLongText(msg.text) && !expandedMessages[msg.id],
            }"
          ></div>

          <button
            v-if="isLongText(msg.text)"
            @click="toggleExpand(msg.id)"
            class="mt-2 pt-1.5 border-t border-zinc-200/40 w-full flex items-center justify-center space-x-1 text-[11px] font-semibold text-zinc-500 hover:text-zinc-900 transition-colors"
          >
            <span>{{
              expandedMessages[msg.id]
                ? $t("chat_component.actions.show_less")
                : $t("chat_component.actions.show_more")
            }}</span>
            <ChevronUpIcon v-if="expandedMessages[msg.id]" class="w-3 h-3" />
            <ChevronDownIcon v-else class="w-3 h-3" />
          </button>
        </div>
      </div>

      <div
        v-if="editingMessageId !== msg.id"
        class="flex items-center space-x-2 mt-1 px-1 text-zinc-400 opacity-0 group-hover:opacity-100 transition-opacity duration-200"
      >
        <button
          @click="copyToClipboard(msg.text)"
          class="hover:text-zinc-700 transition-colors"
          :title="$t('chat_component.tooltips.copy')"
        >
          <DocumentDuplicateIcon class="w-3.5 h-3.5" />
        </button>
        <button
          @click="startEdit"
          class="hover:text-zinc-700 transition-colors"
          :title="$t('chat_component.tooltips.edit')"
        >
          <PencilIcon class="w-3.5 h-3.5" />
        </button>
      </div>
    </div>

    <div
      v-else
      class="w-full py-4 pl-5 border-l-2 border-galadril flex flex-col items-start"
    >
      <div
        v-if="msg.text"
        v-html="formattedText"
        class="markdown-body max-w-full text-zinc-900 leading-relaxed break-words"
      ></div>
      <div v-else class="flex items-center space-x-1 py-1.5">
        <div class="w-1.5 h-1.5 bg-galadril rounded-full animate-bounce" />
        <div
          class="w-1.5 h-1.5 bg-galadril rounded-full animate-bounce [animation-delay:0.2s]"
        />
        <div
          class="w-1.5 h-1.5 bg-galadril rounded-full animate-bounce [animation-delay:0.4s]"
        />
      </div>
    </div>

    <div
      v-if="msg.files && msg.files.length > 0"
      class="flex flex-wrap gap-1.5 pt-1 w-full"
      :class="
        msg.role === 'user'
          ? 'justify-end max-w-[85%] ml-auto'
          : 'pl-5 justify-start'
      "
    >
      <UtilsButton
        v-for="f in msg.files"
        :key="f.name"
        variant="secondary"
        @click="emit('preview-file', f)"
        class="!px-2.5 !py-1 rounded-lg shadow-sm !text-[11px]"
      >
        <DocumentTextIcon class="w-3.5 h-3.5 text-zinc-400" />
        <span class="truncate max-w-[140px] text-zinc-600">{{ f.name }}</span>
      </UtilsButton>
    </div>
  </div>
</template>

<style>
@import "katex/dist/katex.min.css";

.markdown-body {
  font-size: 14px;
}
.markdown-body p {
  margin-bottom: 0.5rem;
}
.markdown-body p:last-child {
  margin-bottom: 0;
}
.markdown-body strong {
  font-weight: 600;
}
.markdown-body em {
  font-style: italic;
}
.markdown-body ul {
  list-style-type: disc;
  padding-left: 1.25rem;
  margin-bottom: 0.5rem;
}
.markdown-body ol {
  list-style-type: decimal;
  padding-left: 1.25rem;
  margin-bottom: 0.5rem;
}
.markdown-body table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.75rem 0;
  font-size: 12px;
}
.markdown-body th,
.markdown-body td {
  border: 1px solid #e4e4e7;
  padding: 0.375rem 0.625rem;
  text-align: left;
}
.markdown-body th {
  background-color: #f4f4f5;
  font-weight: 600;
}
.markdown-body code:not(pre code) {
  background-color: #f4f4f5;
  padding: 0.125rem 0.25rem;
  border-radius: 0.25rem;
  font-family: monospace;
  font-size: 13px;
}
.markdown-body pre {
  background-color: #18181b;
  color: #f4f4f5;
  padding: 0.75rem;
  border-radius: 0.5rem;
  overflow-x: auto;
  margin: 0.5rem 0;
  font-family: monospace;
  font-size: 13px;
}
.markdown-body .katex-display {
  margin: 1rem 0;
  padding: 0.25rem 0;
  overflow-x: auto;
  overflow-y: hidden;
  text-align: center;
}
</style>
