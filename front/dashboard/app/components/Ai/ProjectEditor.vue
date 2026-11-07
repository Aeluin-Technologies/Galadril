<script setup lang="ts">
const { t } = useI18n();

import { onClickOutside } from "@vueuse/core";
import { ChevronDownIcon } from "@heroicons/vue/24/outline";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { projectIcon, projectIcons } from "~/utils/project-icons";
import { usePengoloAgents } from "~/stores/usePengoloAgents";
import { executionContexts } from "~/utils/pengolo";
import type { ProjectRecord } from "~/utils/pengolo-api";
const props = defineProps<{ open: boolean; project?: ProjectRecord | null }>();
const emit = defineEmits<{ close: [] }>();
const projects = usePengoloProjects();
const agents = usePengoloAgents();
const contexts = computed(() => executionContexts(agents.catalog));
const name = ref("");
const icon = ref("folder");
const color = ref("#d97706");
const shared = ref(false);
const roles = ref("");
const contextMode = ref<"project" | "personal">("project");
const executionContext = ref<string>("");
const instructions = ref("");
const appearance = ref(false);
const appearancePanel = ref<HTMLElement | null>(null);
onClickOutside(appearancePanel, () => { appearance.value = false; }, {
  ignore: ["[data-project-appearance-trigger]"],
});
const colors = [
  "#d97706",
  "#ef4444",
  "#f97316",
  "#eab308",
  "#16a34a",
  "#2563eb",
  "#9333ea",
  "#db2777",
];
const busy = ref(false);
const error = ref("");
watch(
  () => props.open,
  (open) => {
    if (!open) return;
    agents.load().catch(() => { error.value = t("pengolo.copy.agents_and_skills_could_not_be_loaded_you"); });
    const project = props.project;
    name.value = project?.data.name ?? "";
    icon.value = projectIcon(project?.data.icon ?? "folder").id;
    color.value = project?.data.color ?? "#d97706";
    shared.value = project?.data.shared ?? false;
    roles.value = project?.data.roles.join(", ") ?? "";
    contextMode.value = project?.data.contextMode ?? "project";
    executionContext.value = project?.data.executionContext ?? "";
    instructions.value = project?.data.instructions ?? "";
    error.value = "";
    appearance.value = false;
  },
);
watch(shared, (value) => {
  if (value) contextMode.value = "project";
});
watch(executionContext, (value) => {
  if (value) contextMode.value = "project";
});
async function save() {
  busy.value = true;
  error.value = "";
  try {
    await projects.save({
      id: props.project?.id ?? null,
      revision: props.project?.revision ?? null,
      name: name.value,
      icon: icon.value,
      color: color.value,
      shared: shared.value,
      roles: shared.value
        ? roles.value
            .split(",")
            .map((role) => role.trim())
            .filter(Boolean)
        : [],
      contextMode: contextMode.value,
      executionContext: executionContext.value || null,
      instructions: instructions.value,
    });
    emit("close");
  } catch {
    error.value = t(
      "pengolo.copy.the_project_could_not_be_saved_preserve_access",
    );
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <UiDialog
    :open="open"
    :title="
      project
        ? $t('pengolo.copy.edit_project')
        : $t('pengolo.copy.create_project')
    "
    @close="emit('close')"
  >
    <form id="pengolo-project" class="p-form" @submit.prevent="save">
      <div class="p-project-name">
        <label for="pengolo-project-name">{{
          $t("pengolo.copy.project_name")
        }}</label>
        <div class="p-name-field">
          <UiButton
            variant="ghost"
            data-project-appearance-trigger
            type="button"
            :aria-label="$t('pengolo.copy.customize_project_appearance')"
            :aria-expanded="appearance"
            :style="{ color }"
            @click="appearance = !appearance"
          >
            <AiProjectIcon
              :name="icon"
              :color="color" /><ChevronDownIcon /></UiButton
          ><UiInput
            id="pengolo-project-name"
            v-model="name"
            autofocus
            required
            maxlength="128"
            :placeholder="$t('pengolo.copy.project_name')"
          />
        </div>
        <div v-if="appearance" ref="appearancePanel" class="p-appearance-picker" @keydown.esc.stop="appearance = false">
          <div class="p-color-grid">
            <UiButton
              variant="ghost"
              v-for="item in colors"
              :key="item"
              type="button"
              :aria-label="
                $t('pengolo.copy.use_color_value0', { value0: item })
              "
              :aria-pressed="color === item"
              :style="{ background: item }"
              @click="color = item"
            /><input
              v-model="color"
              type="color"
              :aria-label="$t('pengolo.copy.project_color')"
            />
          </div>
          <div class="p-icon-grid">
            <UiButton
              variant="ghost"
              v-for="item in projectIcons"
              :key="item.id"
              type="button"
              :style="{ color }"
              :aria-label="$t('projectActions.useIcon', { name: item.label })"
              :aria-pressed="icon === item.id"
              @click="icon = item.id"
            >
              <AiProjectIcon :name="item.id" :color="color" />
            </UiButton>
          </div>
        </div>
      </div>
      <label
        >{{ $t("administration.copy.access")
        }}<UiSelect
          v-model="shared"
          :disabled="!!project"
          :options="[
            {
              label: $t('pengolo.copy.personal_only_you').trim(),
              value: false,
            },
            {
              label: $t('pengolo.copy.shared_selected_galadril_roles').trim(),
              value: true,
            },
          ]"
      /></label>
      <label v-if="shared"
        >{{ $t("administration.copy.roles")
        }}<UiInput
          v-model="roles"
          required
          :placeholder="$t('pengolo.copy.analyst_operations')"
        /><small>{{
          $t(
            "pengolo.copy.use_existing_galadril_role_names_separated_by_commas",
          )
        }}</small></label
      >
      <details class="text-sm">
        <summary class="cursor-pointer text-muted">{{ $t("pengolo.execution.advanced") }}</summary>
      <label class="mt-3">
        {{ $t("pengolo.execution.label") }}
        <UiSelect
          v-model="executionContext"
          :disabled="!!project"
          :options="[{ value: '', label: $t('pengolo.execution.general') }, ...contexts.map(context => ({ value: context.id, label: context.name }))]"
        />
      </label>
      <label
        >{{ $t("pengolo.copy.document_context")
        }}<UiSelect
          v-model="contextMode"
          :disabled="shared || !!executionContext"
          :options="[
            {
              label: $t('pengolo.copy.project_library_only').trim(),
              value: 'project',
            },
            ...(!shared && !executionContext
              ? [
                  {
                    label: $t('pengolo.copy.personal_library_only').trim(),
                    value: 'personal',
                  },
                ]
              : []),
          ]"
      /></label>
      </details>
      <p v-if="!executionContext" class="p-muted">
        {{
          $t(
            "pengolo.copy.authorized_galadril_graph_tools_remain_available_in_every",
          )
        }}
      </p>
      <p v-if="error" role="alert" class="p-error">{{ error }}</p>
      <UiTextarea
        class="p-project-instructions"
        v-model="instructions"
        :label="$t('personalization.projectLabel')"
        :help="
          $t(
            shared
              ? 'personalization.sharedHelp'
              : 'personalization.projectHelp',
          )
        "
        maxlength="2000"
        rows="3"
      />
    </form>
    <template #footer
      ><UiButton variant="secondary" type="button" @click="emit('close')">
        {{ $t("builder.config_modal.buttons.cancel") }}</UiButton
      ><UiButton
        variant="primary"
        type="submit"
        form="pengolo-project"
        :loading="busy"
        :disabled="!name.trim()"
      >
        {{ busy ? $t("pengolo.copy.saving") : $t("pengolo.copy.save_project") }}
      </UiButton></template
    >
  </UiDialog>
</template>
