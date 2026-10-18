<script setup lang="ts">
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
      error.value =
        "Agents and Skills could not be loaded. You can continue without specialists.";
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
    error.value =
      "The agent could not be saved. Check its Skills and project access, then retry.";
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
    error.value = "The agent could not be removed.";
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <AiDialog :open="open" title="Specialist agents" @close="emit('close')">
    <p>
      Choose up to eight specialists for this conversation. Each follows its own
      mission and Skills; the orchestrator combines their findings.
    </p>
    <p v-if="loading" role="status">Loading agents and Skills…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <ul class="p-agent-list">
      <li v-for="agent in agents.items" :key="agent.id">
        <label>
          <input
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
        <small v-if="!compatible(agent)"
          >Unavailable for this project or model, or a required Skill is
          missing.</small
        >
        <small v-else-if="agent.projectId">Restricted to its project.</small>
        <UtilsButton
          variant="ghost"
          type="button"
          :disabled="saving || (!!agent.projectId && agent.projectId !== scope)"
          @click="edit(agent)"
          >Edit</UtilsButton
        >
        <UtilsButton
          variant="ghost"
          type="button"
          :disabled="saving"
          @click="remove(agent)"
          >Remove</UtilsButton
        >
      </li>
    </ul>
    <form class="p-agent-editor" @submit.prevent="save">
      <h3>{{ editing ? "Edit specialist" : "Create a specialist" }}</h3>
      <label
        >Name<input v-model="name" required maxlength="128" :disabled="saving"
      /></label>
      <label
        >Mission<textarea
          v-model="mission"
          required
          maxlength="8192"
          rows="4"
          :disabled="saving"
          placeholder="For example: monitor French administrative case law, verify decisions and cite official sources."
        />
      </label>
      <label v-if="scope"
        ><input v-model="projectOnly" type="checkbox" :disabled="saving" />Only
        in this project</label
      >
      <fieldset :disabled="saving || loading">
        <legend>Skills</legend>
        <p v-if="!skills.length">
          No Skills are installed. Plugins can provide specialized workflows.
        </p>
        <label v-for="skill in skills" :key="skill.id">
          <input
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
            >{{ skill.pluginId ?? "User" }} · {{ skill.description }}</small
          >
          <small v-if="skill.compatibility.allowedModels.length"
            >Models: {{ skill.compatibility.allowedModels.join(", ") }}</small
          >
        </label>
      </fieldset>
      <UtilsButton
        type="submit"
        :disabled="saving || loading || !name.trim() || !mission.trim()"
        >{{ saving ? "Saving…" : "Save agent" }}</UtilsButton
      >
      <UtilsButton
        v-if="editing"
        variant="ghost"
        type="button"
        :disabled="saving"
        @click="edit(null)"
        >New agent</UtilsButton
      >
    </form>
  </AiDialog>
</template>
