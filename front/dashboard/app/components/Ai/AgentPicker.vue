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
function select(agent: Specialist, event: Event) {
  if (!(event.target instanceof HTMLInputElement)) return;
  if (!event.target.checked)
    agents.selected = agents.selected.filter((id) => id !== agent.id);
  else if (compatible(agent) && agents.selected.length < 8)
    agents.selected.push(agent.id);
}
function edit(agent: Specialist | null) {
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
    :title="$t('pengolo.copy.specialist_agents')"
    @close="emit('close')"
  >
    <p>
      {{
        $t("pengolo.copy.choose_up_to_eight_specialists_for_this_conversation")
      }}
    </p>
    <p v-if="loading" role="status">
      {{ $t("pengolo.copy.loading_agents_and_skills") }}
    </p>
    <p v-if="error" role="alert">{{ error }}</p>
    <ul class="p-agent-list">
      <li v-for="agent in agents.items" :key="agent.id">
        <label>
          <UiCheckbox
            type="checkbox"
            :checked="agents.selected.includes(agent.id)"
            :disabled="
              saving ||
              !compatible(agent) ||
              (!agents.selected.includes(agent.id) &&
                agents.selected.length >= 8)
            "
            @change="select(agent, $event)"
          />
          <strong>{{ agent.data.name }}</strong>
        </label>
        <p>{{ agent.data.mission }}</p>
        <small v-if="!compatible(agent)">{{
          $t("pengolo.copy.unavailable_for_this_project_or_model_or_a")
        }}</small>
        <small v-else-if="agent.projectId">{{
          $t("pengolo.copy.restricted_to_its_project")
        }}</small>
        <UiButton
          variant="ghost"
          type="button"
          @click="edit(agent)"
          :loading="saving"
          :disabled="!!agent.projectId && agent.projectId !== scope"
          >{{ $t("ui.actions.edit") }}</UiButton
        >
        <UiButton
          variant="ghost"
          type="button"
          @click="remove(agent)"
          :loading="saving"
          >{{ $t("administration.copy.remove") }}</UiButton
        >
      </li>
    </ul>
    <form class="p-agent-editor" @submit.prevent="save">
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
      <fieldset :disabled="saving || loading">
        <legend>{{ $t("pengolo.copy.skills") }}</legend>
        <p v-if="!skills.length">
          {{
            $t(
              "pengolo.copy.no_skills_are_installed_plugins_can_provide_specialized",
            )
          }}
        </p>
        <label v-for="skill in skills" :key="skill.id">
          <UiCheckbox
            v-model="skillIds"
            type="checkbox"
            :value="skill.id"
            :disabled="
              skill.execution !== 'REGISTERED' ||
              (!skillIds.includes(skill.id) && skillIds.length >= 8)
            "
          />
          {{ skill.name
          }}<small
            >{{ skill.pluginId ?? $t("storage.permissions.user") }} ·
            {{ skill.description }}</small
          >
          <small v-if="skill.compatibility.allowedModels.length"
            >{{ $t("pengolo.copy.models") }}
            {{ skill.compatibility.allowedModels.join(", ") }}</small
          >
        </label>
      </fieldset>
      <UiButton
        type="submit"
        variant="primary"
        :loading="saving"
        :disabled="loading || !name.trim() || !mission.trim()"
        >{{
          saving ? $t("pengolo.copy.saving") : $t("pengolo.copy.save_agent")
        }}</UiButton
      >
      <UiButton
        v-if="editing"
        variant="ghost"
        type="button"
        @click="edit(null)"
        :loading="saving"
        >{{ $t("pengolo.copy.new_agent") }}</UiButton
      >
    </form>
  </UiDialog>
</template>
