<script setup lang="ts">
const { t } = useI18n();

import {
  PlusIcon,
  ArrowUpIcon,
  StopIcon,
  PaperClipIcon,
  GlobeAltIcon,
  ShareIcon,
  UserGroupIcon,
  XMarkIcon,
} from "@heroicons/vue/24/outline";
import { onClickOutside } from "@vueuse/core";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { usePengoloModels } from "~/stores/usePengoloModels";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloAgents } from "~/stores/usePengoloAgents";
import { usePengoloLibrary } from "~/stores/usePengoloLibrary";
import type { FileRecord } from "~/utils/pengolo-api";
const props = defineProps<{ busy?: boolean }>();
const emit = defineEmits<{ submit: []; stop: [] }>();
const ui = usePengoloUi();
const models = usePengoloModels();
const modelOptionsKey = computed(() =>
  JSON.stringify(models.models.map((model) => model.alias)),
);
const library = usePengoloLibrary();
const menu = ref(false);
const menuElement = ref<HTMLElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const projects = usePengoloProjects();
const agents = usePengoloAgents();
const agentPicker = ref(false);
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
watch(
  () => ui.draftVersion,
  () => {
    menu.value = false;
    picker.value = false;
    agentPicker.value = false;
  },
);
onClickOutside(menuElement, () => {
  menu.value = false;
});
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
      ui.error = t("pengolo.copy.a_message_supports_up_to_eight_files");
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
      ui.error = t("pengolo.copy.upload_failed_use_pdf_text_csv_png_jpeg");
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
      <span v-if="ui.graphContext" class="p-chip" role="status">
        <ShareIcon aria-hidden="true" />{{ ui.graphContext.kind }} ·
        {{ ui.graphContext.id }}
        <UiIconButton
          :icon="XMarkIcon"
          :label="$t('pengolo.copy.remove_data_context')"
          :disabled="busy"
          @click="ui.graphContext = null"
        />
      </span>
      <span v-if="ui.webEnabled" class="p-chip"
        ><GlobeAltIcon />{{ $t("pengolo.copy.web_search")
        }}<UiButton
          variant="ghost"
          type="button"
          :aria-label="$t('pengolo.copy.disable_web_search')"
          @click="ui.webEnabled = false"
          :loading="busy"
        >
          ×
        </UiButton></span
      >
      <span v-for="file in ui.files" :key="file.id" class="p-chip"
        ><PaperClipIcon />{{ file.data.name
        }}<UiButton
          variant="ghost"
          type="button"
          :aria-label="
            $t('pengolo.copy.remove_value0', { value0: file.data.name })
          "
          @click="ui.files = ui.files.filter((item) => item.id !== file.id)"
          :loading="busy"
        >
          ×
        </UiButton></span
      >
    </div>
    <UiTextarea
      variant="composer"
      autosize
      v-model="ui.prompt"
      :aria-label="$t('pengolo.copy.message_pengolo')"
      :placeholder="$t('pengolo.copy.ask_pengolo')"
      rows="1"
      maxlength="32768"
      :disabled="busy"
      @keydown="keydown"
    />
    <div class="p-composer-actions">
      <div ref="menuElement" class="p-tools">
        <UiIconButton
          variant="ghost"
          type="button"
          :aria-expanded="menu"
          @click="menu = !menu"
          :icon="PlusIcon"
          :label="$t('pengolo.copy.add_files_and_tools')"
          :loading="busy"
        /><Transition name="p-pop"
          ><div
            v-if="menu"
            class="p-menu"
            role="menu"
            @keydown.esc="menu = false"
          >
            <UiButton
              variant="ghost"
              type="button"
              role="menuitem"
              align="start"
              class="w-full"
              @click="
                agentPicker = true;
                menu = false;
              "
              ><UserGroupIcon aria-hidden="true" />{{
                $t("pengolo.copy.specialist_agents")
              }}<span>{{ agents.selected.length || "" }}</span></UiButton
            >
            <UiButton
              variant="ghost"
              type="button"
              role="menuitem"
              align="start"
              class="w-full"
              @click="fileInput?.click()"
            >
              <PaperClipIcon />{{ $t("pengolo.copy.attach_files") }}</UiButton
            ><UiButton
              variant="ghost"
              type="button"
              role="menuitem"
              align="start"
              class="w-full"
              :disabled="ui.temporary && !ui.withLibrary"
              @click="
                picker = true;
                menu = false;
              "
            >
              <PaperClipIcon />{{
                $t("pengolo.copy.choose_from_library")
              }}</UiButton
            ><UiButton
              variant="ghost"
              type="button"
              role="menuitem"
              align="start"
              class="w-full"
              :disabled="!ui.webAvailable"
              :title="
                ui.webAvailable
                  ? $t(
                      'pengolo.copy.let_pengolo_search_and_read_public_sources',
                    )
                  : $t(
                      'pengolo.copy.web_search_is_not_configured_for_this_workspace',
                    )
              "
              @click="
                ui.webEnabled = !ui.webEnabled;
                menu = false;
              "
            >
              <GlobeAltIcon />{{ $t("pengolo.copy.web_search")
              }}<span>{{
                ui.webEnabled ? $t("pengolo.copy.on") : ""
              }}</span></UiButton
            >
          </div></Transition
        >
      </div>
      <input
        ref="fileInput"
        class="sr-only"
        type="file"
        :aria-label="$t('pengolo.copy.attach_files')"
        multiple
        accept=".pdf,.txt,.csv,.png,.jpg,.jpeg,.webp"
        @change="upload"
      />
      <span class="p-spacer" />
      <span v-if="agents.selected.length" class="p-chip"
        >{{ agents.selected.length }}
        {{ $t("pengolo.copy.specialist_s") }}</span
      >
      <UiSelect
        variant="inline"
        class="max-w-40"
        :key="modelOptionsKey"
        v-model="models.selected"
        :aria-label="$t('pengolo.copy.model')"
        :disabled="busy || models.loading || !models.models.length"
        :options="[
          ...models.models.flatMap((model) => [
            {
              label: String(model.model || model.alias).trim(),
              value: model.alias,
            },
          ]),
          ...(!models.models.length
            ? [
                {
                  label: String(
                    models.loading
                      ? $t('pengolo.copy.loading_models')
                      : $t('pengolo.copy.no_compatible_models'),
                  ).trim(),
                  value: '',
                },
              ]
            : []),
        ]"
      />
      <span
        class="p-medium"
        :title="$t('pengolo.copy.reasoning_is_always_enabled_at_medium')"
        >{{ $t("pengolo.copy.medium") }}</span
      >
      <span v-if="library.uploading" class="p-muted" role="status">{{
        $t("pengolo.copy.uploading")
      }}</span>
      <UiIconButton
        variant="primary"
        v-if="busy"
        type="button"
        @click="emit('stop')"
        :icon="StopIcon"
        :label="$t('pengolo.copy.stop_generation')"
      /><UiIconButton
        variant="primary"
        v-else
        type="submit"
        :tooltip="false"
        :disabled="
          !ui.prompt.trim() || !models.selected || library.uploading > 0
        "
        :icon="ArrowUpIcon"
        :label="$t('pengolo.copy.send_message')"
      />
    </div>
    <UiDialog
      :open="picker"
      :title="$t('pengolo.copy.attach_from_library')"
      @close="picker = false"
      ><AiLibrary
        v-if="picker"
        :project-id="documentProject"
        picker
        @select="select"
    /></UiDialog>
    <AiAgentPicker :open="agentPicker" @close="agentPicker = false" />
  </form>
</template>
