<script setup lang="ts">
import {
  EllipsisHorizontalIcon,
  PencilSquareIcon,
  TrashIcon,
} from "@heroicons/vue/24/outline";
import type { ProjectRecord } from "~/utils/pengolo-api";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { usePengoloChats } from "~/stores/usePengoloChats";
const props = defineProps<{ project: ProjectRecord; editable: boolean }>();
const emit = defineEmits<{ edit: []; toggle: [event: Event] }>();
const projects = usePengoloProjects(),
  ui = usePengoloUi(),
  chats = usePengoloChats();
const route = useRoute();
const { t } = useI18n();
const deleting = ref(false),
  busy = ref(false);
const items = computed(() => [
  {
    id: "edit",
    label: t("pengolo.copy.rename_and_permissions"),
    icon: PencilSquareIcon,
  },
  {
    id: "delete",
    label: t("projectActions.delete"),
    icon: TrashIcon,
    tone: "danger" as const,
  },
]);
function select(id: string) {
  if (id === "edit") emit("edit");
  else if (id === "delete") deleting.value = true;
}
async function remove() {
  if (busy.value) return;
  busy.value = true;
  try {
    await projects.remove(props.project);
    deleting.value = false;
    if (
      ui.projectId === props.project.id ||
      chats.active?.projectId === props.project.id ||
      route.query.project === props.project.id
    ) {
      chats.active = null;
      ui.draft();
      await navigateTo("/pengolo");
    }
  } catch {
    ui.error = t("projectActions.failed");
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <div class="p-project">
    <details @toggle="emit('toggle', $event)">
      <summary>
        <AiProjectIcon
          :name="project.data.icon"
          :color="project.data.color"
        /><span class="p-project-title">{{ project.data.name }}</span>
      </summary>
      <slot />
    </details>
    <UiMenu
      v-if="editable"
      class="p-project-options"
      :label="
        $t('pengolo.copy.options_for_project_value0', {
          value0: project.data.name,
        })
      "
      :items="items"
      :trigger-icon="EllipsisHorizontalIcon"
      icon-only
      variant="ghost"
      @select="select"
    />
    <UiDialog
      :open="deleting"
      :title="$t('projectActions.confirm')"
      :dismissible="!busy"
      @close="deleting = false"
    >
      <p>{{ $t("projectActions.description", { name: project.data.name }) }}</p>
      <template #footer
        ><UiButton
          variant="secondary"
          :disabled="busy"
          @click="deleting = false"
          >{{ $t("projectActions.keep") }}</UiButton
        ><UiButton variant="danger" :loading="busy" @click="remove"
          ><TrashIcon aria-hidden="true" />{{
            $t("projectActions.delete")
          }}</UiButton
        ></template
      >
    </UiDialog>
  </div>
</template>
