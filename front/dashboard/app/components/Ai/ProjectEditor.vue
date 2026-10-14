<script setup lang="ts">
import { FolderIcon, ChevronDownIcon } from "@heroicons/vue/24/outline";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import type { ProjectRecord } from "~/utils/pengolo-api";
const props = defineProps<{ open: boolean; project?: ProjectRecord | null }>();
const emit = defineEmits<{ close: [] }>();
const projects = usePengoloProjects();
const name = ref("");
const icon = ref("📁");
const color = ref("#d97706");
const shared = ref(false);
const roles = ref("");
const contextMode = ref<"project" | "personal">("project");
const appearance = ref(false);
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
const icons = [
  "📁",
  "📊",
  "🔎",
  "🧭",
  "🌍",
  "⚖️",
  "💡",
  "🧪",
  "🛠️",
  "📈",
  "🏢",
  "🛰️",
];
watch(
  () => props.open,
  (open) => {
    if (!open) return;
    const project = props.project;
    name.value = project?.data.name ?? "";
    icon.value = project?.data.icon ?? "📁";
    color.value = project?.data.color ?? "#d97706";
    shared.value = project?.data.shared ?? false;
    roles.value = project?.data.roles.join(", ") ?? "";
    contextMode.value = project?.data.contextMode ?? "project";
    error.value = "";
    appearance.value = false;
  },
);
watch(shared, (value) => {
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
    });
    emit("close");
  } catch {
    error.value =
      "The project could not be saved. Check its roles and try again.";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <AiDialog
    :open="open"
    :title="project ? 'Edit project' : 'Create project'"
    @close="emit('close')"
  >
    <form id="pengolo-project" class="p-form" @submit.prevent="save">
      <div class="p-project-name">
        <label for="pengolo-project-name">Project name</label>
        <div class="p-name-field">
          <UtilsButton
            variant="ghost"
            type="button"
            class="p-appearance-trigger"
            aria-label="Customize project appearance"
            :aria-expanded="appearance"
            :style="{ color }"
            @click="appearance = !appearance"
          >
            <FolderIcon v-if="icon === '📁'" /><span v-else>{{ icon }}</span
            ><ChevronDownIcon /></UtilsButton
          ><input
            id="pengolo-project-name"
            v-model="name"
            autofocus
            required
            maxlength="128"
            placeholder="Project name"
          />
        </div>
        <div v-if="appearance" class="p-appearance-picker">
          <div class="p-color-grid">
            <UtilsButton
              variant="ghost"
              v-for="item in colors"
              :key="item"
              type="button"
              :aria-label="`Use color ${item}`"
              :aria-pressed="color === item"
              :style="{ background: item }"
              @click="color = item"
            /><input v-model="color" type="color" aria-label="Project color" />
          </div>
          <div class="p-icon-grid">
            <UtilsButton
              variant="ghost"
              v-for="item in icons"
              :key="item"
              type="button"
              :aria-label="`Select ${item}`"
              :aria-pressed="icon === item"
              class="p-icon"
              @click="icon = item"
            >
              <FolderIcon v-if="item === '📁'" /><span v-else>{{ item }}</span>
            </UtilsButton>
          </div>
          <label
            >Custom emoji<input
              v-model="icon"
              maxlength="16"
              aria-label="Custom project emoji"
          /></label>
        </div>
      </div>
      <label
        >Access<select v-model="shared" :disabled="!!project">
          <option :value="false">Personal · only you</option>
          <option :value="true">Shared · selected Galadril roles</option>
        </select></label
      >
      <label v-if="shared"
        >Roles<input
          v-model="roles"
          required
          placeholder="analyst, operations"
        /><small
          >Use existing Galadril role names, separated by commas.</small
        ></label
      >
      <label
        >Document context<select v-model="contextMode" :disabled="shared">
          <option value="project">Project library only</option>
          <option v-if="!shared" value="personal">Personal library only</option>
        </select></label
      >
      <p class="p-muted">
        Authorized Galadril graph tools remain available in every project.
      </p>
      <p v-if="error" role="alert" class="p-error">{{ error }}</p>
    </form>
    <template #footer
      ><UtilsButton
        variant="secondary"
        type="button"
        class="p-button"
        @click="emit('close')"
      >
        Cancel</UtilsButton
      ><UtilsButton
        variant="primary"
        type="submit"
        form="pengolo-project"
        class="p-button primary"
        :disabled="busy || !name.trim()"
      >
        {{ busy ? "Saving…" : "Save project" }}
      </UtilsButton></template
    >
  </AiDialog>
</template>
