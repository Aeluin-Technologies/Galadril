<script setup lang="ts">
const { t } = useI18n();

import { usePengoloAgents, type Specialist } from "~/stores/usePengoloAgents";
import { usePengoloModels } from "~/stores/usePengoloModels";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { compatibleAgent } from "~/utils/pengolo";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();
const agents = usePengoloAgents();
const models = usePengoloModels();
const ui = usePengoloUi();
const name = ref("");
const mission = ref("");
const skillIds = ref<string[]>([]);
const projectOnly = ref(false);
const editing = ref<Specialist | null>(null);
const mode = ref<"select" | "manage" | "edit">("select");
const options = computed(() =>
  agents.items.map((agent) => ({
    value: agent.id,
    label: agent.data.name,
    description:
      (agent.data.pluginId ? `Plugin ${agent.data.pluginId}: ` : "") +
      agent.data.mission,
    disabled:
      !compatible(agent) ||
      (agents.selected.length >= 8 && !agents.selected.includes(agent.id)),
  })),
);
const skillOptions = computed(() =>
  skills.value.map((skill) => ({
    value: skill.id,
    label: skill.name,
    description: skill.description,
    disabled:
      skill.execution !== "REGISTERED" ||
      (skillIds.value.length >= 8 && !skillIds.value.includes(skill.id)),
  })),
);
const loading = ref(false);
const saving = ref(false);
const error = ref("");
const skills = computed(() =>
  agents.catalog.filter((item) => item.kind === "SKILL"),
);
const scope = computed(() => (ui.temporary ? null : ui.projectId));
watch(
  () => props.open,
  async (open) => {
    if (!open) return;
    mode.value = "select";
    loading.value = true;
    error.value = "";
    try {
      await agents.load();
    } catch {
      error.value = t("pengolo.copy.agents_and_skills_could_not_be_loaded_you");
    } finally {
      loading.value = false;
    }
  },
);
function compatible(agent: Specialist) {
  return compatibleAgent(agent, scope.value, models.selected, agents.catalog);
}
function edit(agent: Specialist | null) {
  mode.value = "edit";
  editing.value = agent;
  name.value = agent?.data.name ?? "";
  mission.value = agent?.data.mission ?? "";
  skillIds.value = [...(agent?.data.skillIds ?? [])];
  projectOnly.value = !!agent?.projectId;
}
async function save() {
  if (saving.value) return;
  saving.value = true;
  error.value = "";
  try {
    await agents.save({
      id: editing.value?.id ?? null,
      revision: editing.value?.revision ?? null,
      name: name.value.trim(),
      mission: mission.value.trim(),
      skillIds: skillIds.value,
      projectId: projectOnly.value ? scope.value : null,
    });
    edit(null);
    mode.value = "manage";
  } catch {
    error.value = t("pengolo.copy.the_agent_could_not_be_saved_check_its");
  } finally {
    saving.value = false;
  }
}
async function remove(agent: Specialist) {
  if (saving.value) return;
  saving.value = true;
  try {
    await agents.remove(agent.id);
    if (editing.value?.id === agent.id) edit(null);
  } catch {
    error.value = t("pengolo.copy.the_agent_could_not_be_removed");
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <UiDialog
    :open="open"
    :title="
      mode === 'select'
        ? $t('pengolo.copy.specialist_agents')
        : mode === 'manage'
          ? $t('pengolo.manageSpecialists')
          : editing
            ? $t('pengolo.copy.edit_specialist')
            : $t('pengolo.copy.create_a_specialist')
    "
    @close="emit('close')"
  >
    <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
    <p v-if="loading" role="status">
      {{ $t("pengolo.copy.loading_agents_and_skills") }}
    </p>
    <div v-if="mode === 'select'" class="space-y-4">
      <p>
        {{
          $t(
            "pengolo.copy.choose_up_to_eight_specialists_for_this_conversation",
          )
        }}
      </p>
      <UiSearchSelect
        :model-value="agents.selected"
        @update:model-value="
          (value) => {
            if (Array.isArray(value) && value.length <= 8)
              agents.selected = value;
          }
        "
        :label="$t('pengolo.copy.specialist_agents')"
        :search-label="$t('pengolo.searchSpecialists')"
        :empty-label="$t('email.noMatches')"
        :options="options"
        multiple
        :disabled="loading"
      />
      <UiButton variant="secondary" @click="mode = 'manage'">{{
        $t("pengolo.manageSpecialists")
      }}</UiButton>
    </div>
    <div v-else-if="mode === 'manage'" class="space-y-4">
      <UiButton variant="ghost" @click="mode = 'select'">{{
        $t("pengolo.backToSelection")
      }}</UiButton>
      <UiButton @click="edit(null)">{{
        $t("pengolo.copy.create_a_specialist")
      }}</UiButton>
      <ul class="space-y-3">
        <li
          v-for="agent in agents.items.filter((item) => !item.data.readOnly)"
          :key="agent.id"
          class="rounded-panel border border-border p-3"
        >
          <strong>{{ agent.data.name }}</strong>
          <p class="text-sm text-muted">{{ agent.data.mission }}</p>
          <div class="mt-2 flex gap-2">
            <UiButton
              variant="secondary"
              :disabled="!!agent.projectId && agent.projectId !== scope"
              @click="edit(agent)"
              >{{ $t("ui.actions.edit") }}</UiButton
            ><UiButton
              variant="ghost"
              :loading="saving"
              @click="remove(agent)"
              >{{ $t("administration.copy.remove") }}</UiButton
            >
          </div>
        </li>
      </ul>
      <p class="text-sm text-muted">
        {{ $t("pengolo.pluginSpecialistsHelp") }}
      </p>
    </div>
    <form v-else class="p-agent-editor" @submit.prevent="save">
      <h3>
        {{
          editing
            ? $t("pengolo.copy.edit_specialist")
            : $t("pengolo.copy.create_a_specialist")
        }}
      </h3>
      <label
        >{{ $t("pengolo.copy.name")
        }}<UiInput v-model="name" required maxlength="128" :disabled="saving"
      /></label>
      <label
        >{{ $t("pengolo.copy.mission")
        }}<UiTextarea
          v-model="mission"
          required
          maxlength="8192"
          rows="4"
          :disabled="saving"
          :placeholder="
            $t(
              'pengolo.copy.for_example_monitor_french_administrative_case_law_verify',
            )
          "
        />
      </label>
      <label v-if="scope"
        ><UiCheckbox
          v-model="projectOnly"
          type="checkbox"
          :disabled="saving"
        />{{ $t("pengolo.copy.only_in_this_project") }}</label
      >
      <UiSearchSelect
        :model-value="skillIds"
        @update:model-value="
          (value) => {
            if (Array.isArray(value) && value.length <= 8) skillIds = value;
          }
        "
        :label="$t('pengolo.copy.skills')"
        :search-label="$t('email.searchSkills')"
        :empty-label="$t('email.noMatches')"
        :options="skillOptions"
        multiple
        :disabled="saving || loading"
      />
      <UiButton
        type="submit"
        variant="primary"
        :loading="saving"
        :disabled="loading || !name.trim() || !mission.trim()"
        >{{
          saving ? $t("pengolo.copy.saving") : $t("pengolo.copy.save_agent")
        }}</UiButton
      >
      <UiButton variant="ghost" :disabled="saving" @click="mode = 'manage'">{{
        $t("actions.cancel")
      }}</UiButton>
    </form>
  </UiDialog>
</template>
