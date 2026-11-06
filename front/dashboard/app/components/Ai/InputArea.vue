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
  CheckIcon,
} from "@heroicons/vue/24/outline";
import { onClickOutside } from "@vueuse/core";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { usePengoloModels } from "~/stores/usePengoloModels";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloAgents } from "~/stores/usePengoloAgents";
import { usePengoloLibrary } from "~/stores/usePengoloLibrary";
import type { MenuItem } from "@galadril/ui/types";
import type { FileRecord } from "~/utils/pengolo-api";
import { modelDisplayName } from "~/utils/pengolo";
const props = defineProps<{ busy?: boolean }>();
const emit = defineEmits<{ submit: []; stop: [] }>();
const ui = usePengoloUi();
const models = usePengoloModels();
const modelLabel = computed(
  () =>
    modelDisplayName(
      models.models.find((model) => model.alias === models.selected)?.model ||
        models.selected,
    ) ||
    t(
      models.loading
        ? "pengolo.copy.loading_models"
        : "pengolo.copy.no_compatible_models",
    ),
);
const modelItems = computed<MenuItem[]>(() => [
  ...models.models.map((model) => ({
    id: `model:${model.alias}`,
    label: modelDisplayName(model.model || model.alias),
    icon: model.alias === models.selected ? CheckIcon : undefined,
  })),
  {
    id: "thinking-effort",
    label: t("pengolo.modelChoice.effort"),
    description: t("pengolo.modelChoice.fixedEffort"),
    disabled: true,
  },
]);
function chooseModel(id: string) {
  const model = models.models.find((model) => `model:${model.alias}` === id);
  if (model) models.selected = model.alias;
}
const library = usePengoloLibrary();
const uploads = usePengoloFileUpload();
const menu = ref(false);
const menuElement = ref<HTMLElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const projects = usePengoloProjects();
const agents = usePengoloAgents();
const teams = useSpecialistTeams();
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
    teams.activeTeams.value.length > 0 ||
    ui.files.length > 0 ||
    !!ui.graphContext ||
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
  const files = Array.from(input.files ?? []);
  input.value = "";
  menu.value = false;
  await uploads.attach(files);
}

function keydown(event: KeyboardEvent) {
  const input = event.target;
  if (
    !props.busy &&
    !event.isComposing &&
    ui.webEnabled &&
    (event.key === "Backspace" || event.key === "Delete") &&
    input instanceof HTMLTextAreaElement &&
    input.selectionStart === 0 &&
    input.selectionEnd === 0
  ) {
    event.preventDefault();
    ui.webEnabled = false;
    return;
  }
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
    <div v-if="ui.graphContext || ui.files.length" class="p-file-chips">
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
    <div class="p-composer-input">
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
      <UiButton
        v-if="ui.webEnabled"
        variant="ghost"
        class="p-active-tool"
        :tooltip="false"
        :aria-label="$t('pengolo.copy.disable_web_search')"
        :disabled="busy"
        @click="ui.webEnabled = false"
      >
        <span class="p-tool-label text-info">
          <span class="p-tool-symbol">
            <GlobeAltIcon class="p-tool-globe" aria-hidden="true" />
            <XMarkIcon class="p-tool-remove" aria-hidden="true" />
          </span>
          {{ $t("pengolo.copy.web_search") }}
        </span>
      </UiButton>
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
    </div>
    <div class="p-composer-actions">
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
      <UiButton
        v-for="team in teams.activeTeams.value"
        :key="team.id"
        variant="ghost"
        size="sm"
        @click="agentPicker = true"
        ><UserGroupIcon class="size-4" aria-hidden="true" />{{
          team.name
        }}</UiButton
      >
      <UiMenu
        variant="ghost"
        :label="modelLabel"
        :aria-label="$t('pengolo.copy.model')"
        class="p-model-trigger"
        :disabled="busy || models.loading || !models.models.length"
        :items="modelItems"
        @select="chooseModel"
      />
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
