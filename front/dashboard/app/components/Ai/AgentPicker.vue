<script setup lang="ts">
const { t } = useI18n();

import { usePengoloAgents, type Specialist } from "~/stores/usePengoloAgents";
import { usePengoloModels } from "~/stores/usePengoloModels";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { ScaleIcon, UserGroupIcon, CheckIcon } from "@heroicons/vue/24/outline";

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
const teams = useSpecialistTeams();
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
      <p class="text-sm text-muted">{{ $t("pengolo.specialists.help") }}</p>
      <p
        v-if="!loading && !teams.teams.value.length"
        class="text-sm text-muted"
      >
        {{ $t("pengolo.specialists.empty") }}
      </p>
      <section
        v-for="team in teams.teams.value"
        :key="team.id"
        :aria-label="team.name"
        class="p-specialist-team"
        :class="{ selected: teams.selected(team) }"
        @click.self="teams.toggle(team)"
      >
        <UiButton
          variant="ghost"
          align="start"
          class="w-full"
          :tooltip="false"
          :aria-label="team.name"
          :aria-pressed="teams.selected(team)"
          :disabled="
            loading || (!teams.selected(team) && !teams.available(team))
          "
          @click="teams.toggle(team)"
        >
          <span class="p-team-heading">
            <component
              :is="team.pluginId === 'law' ? ScaleIcon : UserGroupIcon"
              :class="[
                'size-6 shrink-0',
                team.pluginId === 'law' ? 'text-success' : 'text-info',
              ]"
              aria-hidden="true"
            />
            <span class="min-w-0 flex-1 whitespace-normal text-left">
              <strong class="block text-foreground">{{ team.name }}</strong>
              <span class="mt-1 block text-sm font-normal text-muted">{{
                team.description
              }}</span>
            </span>
            <CheckIcon
              v-if="teams.selected(team)"
              class="size-5 shrink-0 text-primary-foreground"
              aria-hidden="true"
            />
          </span>
        </UiButton>
        <details class="p-team-members">
          <summary>
            {{
              $t("pengolo.specialists.viewMembers", {
                count: team.members.length,
              })
            }}
          </summary>
          <ul class="mt-3 space-y-3">
            <li v-for="member in team.members" :key="member.id">
              <p class="text-sm font-medium">{{ member.data.name }}</p>
              <p class="mt-1 text-sm text-muted">{{ member.data.mission }}</p>
            </li>
          </ul>
        </details>
        <p
          v-if="!teams.selected(team) && !teams.available(team)"
          class="mt-2 text-xs text-muted"
        >
          {{ $t("pengolo.specialists.unavailable") }}
        </p>
      </section>
      <div class="flex items-center justify-end gap-3">
        <UiButton @click="emit('close')">{{
          $t("pengolo.specialists.done")
        }}</UiButton>
      </div>
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

<style scoped>
.p-specialist-team {
  border: 1px solid rgb(var(--ui-border));
  border-radius: var(--ui-radius-panel);
  padding: 12px;
  cursor: pointer;
}
.p-specialist-team.selected {
  border-color: rgb(var(--ui-primary-active));
  background: rgb(var(--ui-primary-subtle));
}
.p-team-heading {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.p-team-members {
  margin: 12px 12px 0;
  font-size: 14px;
  color: rgb(var(--ui-muted));
}
.p-team-members summary {
  cursor: pointer;
  padding: 6px 0;
}
</style>
