<script setup lang="ts">
import {
  PlusIcon,
  ArrowUpIcon,
  StopIcon,
  PaperClipIcon,
  GlobeAltIcon,
  ShareIcon,
} from "@heroicons/vue/24/outline";
import { onClickOutside } from "@vueuse/core";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { usePengoloModels } from "~/stores/usePengoloModels";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloLibrary } from "~/stores/usePengoloLibrary";
import type { FileRecord } from "~/utils/pengolo-api";
const props = defineProps<{ busy?: boolean }>();
const emit = defineEmits<{ submit: []; stop: [] }>();
const ui = usePengoloUi();
const models = usePengoloModels();
const library = usePengoloLibrary();
const menu = ref(false);
const menuElement = ref<HTMLElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const textarea = ref<HTMLTextAreaElement | null>(null);
const projects = usePengoloProjects();
const documentProject = computed(() =>
  ui.temporary ||
  projects.items.find((project) => project.id === ui.projectId)?.data
    .contextMode === "personal"
    ? null
    : ui.projectId,
);
const expanded = computed(
  () =>
    ui.files.length > 0 ||
    !!ui.graphContext ||
    ui.webEnabled ||
    library.uploading > 0 ||
    ui.prompt.includes("\n") ||
    ui.prompt.length > 90,
);
const picker = ref(false);
const contextPicker = ref(false);
watch(
  () => ui.draftVersion,
  () => {
    menu.value = false;
    picker.value = false;
    contextPicker.value = false;
  },
);
onClickOutside(menuElement, () => {
  menu.value = false;
});
watch(
  () => ui.prompt,
  async () => {
    await nextTick();
    if (textarea.value) {
      textarea.value.style.height = "auto";
      textarea.value.style.height = `${Math.min(textarea.value.scrollHeight, 220)}px`;
    }
  },
);
function select(file: FileRecord) {
  if (!ui.files.some((item) => item.id === file.id) && ui.files.length < 8)
    ui.files.push(file);
  picker.value = false;
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const version = ui.draftVersion;
  const scope = documentProject.value;
  const temporary = ui.temporary;
  menu.value = false;
  for (const file of Array.from(input.files ?? [])) {
    if (ui.files.length >= 8) {
      ui.error = "A message supports up to eight files.";
      break;
    }
    try {
      if (temporary && !ui.temporarySession)
        ui.temporarySession = crypto.randomUUID().replaceAll("-", "");
      const ready = await library.upload(
        file,
        scope,
        temporary ? ui.temporarySession : null,
      );
      if (version === ui.draftVersion) select(ready);
    } catch {
      ui.error =
        "Upload failed. Use PDF, text, CSV, PNG, JPEG or WebP files up to 25 MB.";
    }
  }
  input.value = "";
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (
      !props.busy &&
      ui.prompt.trim() &&
      models.selected &&
      !library.uploading
    )
      emit("submit");
  }
}
</script>

<template>
  <form :class="['p-composer', { expanded }]" @submit.prevent="emit('submit')">
    <div
      v-if="ui.graphContext || ui.webEnabled || ui.files.length"
      class="p-file-chips"
    >
      <UtilsButton
        variant="ghost"
        v-if="ui.graphContext"
        type="button"
        class="p-chip context"
        :disabled="busy"
        @click="contextPicker = true"
      >
        <ShareIcon />{{ ui.graphContext.kind }} · {{ ui.graphContext.id }}
      </UtilsButton>
      <span v-if="ui.webEnabled" class="p-chip"
        ><GlobeAltIcon />Web search<UtilsButton
          variant="ghost"
          type="button"
          aria-label="Disable web search"
          :disabled="busy"
          @click="ui.webEnabled = false"
        >
          ×
        </UtilsButton></span
      >
      <span v-for="file in ui.files" :key="file.id" class="p-chip"
        ><PaperClipIcon />{{ file.data.name
        }}<UtilsButton
          variant="ghost"
          type="button"
          :aria-label="`Remove ${file.data.name}`"
          :disabled="busy"
          @click="ui.files = ui.files.filter((item) => item.id !== file.id)"
        >
          ×
        </UtilsButton></span
      >
    </div>
    <textarea
      ref="textarea"
      v-model="ui.prompt"
      aria-label="Message Pengolo"
      placeholder="Ask Pengolo"
      rows="1"
      maxlength="32768"
      :disabled="busy"
      @keydown="keydown"
    />
    <label v-if="ui.webEnabled" class="p-web-query"
      >Public web search query<input
        v-model="ui.webQuery"
        :disabled="busy"
        maxlength="512"
        required
        placeholder="Enter only information that may be sent publicly"
      /><small>Only this query is sent to the search provider.</small></label
    >
    <div class="p-composer-actions">
      <div ref="menuElement" class="p-tools">
        <UtilsButton
          variant="ghost"
          type="button"
          class="p-icon"
          aria-label="Add files and tools"
          :aria-expanded="menu"
          :disabled="busy"
          @click="menu = !menu"
        >
          <PlusIcon /></UtilsButton
        ><Transition name="p-pop"
          ><div
            v-if="menu"
            class="p-menu"
            role="menu"
            @keydown.esc="menu = false"
          >
            <UtilsButton
              variant="ghost"
              type="button"
              role="menuitem"
              @click="fileInput?.click()"
            >
              <PaperClipIcon />Attach files</UtilsButton
            ><UtilsButton
              variant="ghost"
              type="button"
              role="menuitem"
              :disabled="ui.temporary && !ui.withLibrary"
              @click="
                picker = true;
                menu = false;
              "
            >
              <PaperClipIcon />Choose from library</UtilsButton
            ><UtilsButton
              variant="ghost"
              type="button"
              role="menuitem"
              :disabled="!ui.webAvailable"
              :title="
                ui.webAvailable
                  ? 'Use a separate public query'
                  : 'Web search is not configured for this workspace'
              "
              @click="
                ui.webEnabled = !ui.webEnabled;
                menu = false;
              "
            >
              <GlobeAltIcon />Web search<span>{{
                ui.webEnabled ? "On" : ""
              }}</span></UtilsButton
            ><UtilsButton
              variant="ghost"
              type="button"
              role="menuitem"
              @click="
                contextPicker = true;
                menu = false;
              "
            >
              <ShareIcon />Galadril Graph Context
            </UtilsButton>
          </div></Transition
        >
      </div>
      <input
        ref="fileInput"
        class="sr-only"
        type="file"
        multiple
        accept=".pdf,.txt,.csv,.png,.jpg,.jpeg,.webp"
        @change="upload"
      />
      <span class="p-spacer" />
      <select
        v-model="models.selected"
        aria-label="Model"
        :disabled="busy || models.loading || !models.models.length"
      >
        <option
          v-for="model in models.models"
          :key="model.alias"
          :value="model.alias"
        >
          {{ model.model || model.alias }}
        </option>
        <option v-if="!models.models.length" value="">
          {{ models.loading ? "Loading models…" : "No compatible models" }}
        </option>
      </select>
      <span class="p-medium" title="Reasoning is always enabled at Medium"
        >Medium</span
      >
      <span v-if="library.uploading" class="p-muted" role="status"
        >Uploading…</span
      >
      <UtilsButton
        variant="primary"
        v-if="busy"
        type="button"
        class="p-send"
        aria-label="Stop generation"
        @click="emit('stop')"
      >
        <StopIcon /></UtilsButton
      ><UtilsButton
        variant="primary"
        v-else
        type="submit"
        class="p-send"
        aria-label="Send message"
        :tooltip="false"
        :disabled="
          !ui.prompt.trim() || !models.selected || library.uploading > 0
        "
      >
        <ArrowUpIcon />
      </UtilsButton>
    </div>
    <AiDialog :open="picker" title="Attach from library" @close="picker = false"
      ><AiLibrary :project-id="documentProject" picker @select="select"
    /></AiDialog>
    <AiGraphContext :open="contextPicker" @close="contextPicker = false" />
  </form>
</template>
