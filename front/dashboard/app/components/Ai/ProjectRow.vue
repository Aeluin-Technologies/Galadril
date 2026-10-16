<script setup lang="ts">
import { EllipsisHorizontalIcon } from "@heroicons/vue/24/outline";
import { onClickOutside } from "@vueuse/core";
import type { ProjectRecord } from "~/utils/pengolo-api";

defineProps<{ project: ProjectRecord; editable: boolean }>();
const emit = defineEmits<{ edit: []; toggle: [event: Event] }>();
const menu = ref(false);
const container = ref<HTMLElement | null>(null);
const trigger = ref<{ $el: HTMLButtonElement } | null>(null);
onClickOutside(container, () => {
  menu.value = false;
});
function close() {
  menu.value = false;
  trigger.value?.$el.focus();
}
</script>

<template>
  <div ref="container" class="p-project" @keydown.esc="close">
    <details @toggle="emit('toggle', $event)">
      <summary>
        <span :style="{ color: project.data.color }">{{
          project.data.icon
        }}</span>
        <span class="p-project-title">{{ project.data.name }}</span>
      </summary>
      <slot />
    </details>
    <UtilsButton
      v-if="editable"
      ref="trigger"
      variant="ghost"
      type="button"
      class="p-icon p-project-options"
      :aria-label="`Options for project ${project.data.name}`"
      :aria-expanded="menu"
      aria-haspopup="menu"
      @click="menu = !menu"
      ><EllipsisHorizontalIcon
    /></UtilsButton>
    <div
      v-if="menu && editable"
      class="p-menu p-project-menu"
      role="menu"
      :aria-label="`Manage ${project.data.name}`"
    >
      <UtilsButton
        variant="ghost"
        type="button"
        role="menuitem"
        @click="
          close();
          emit('edit');
        "
      >
        Rename and permissions
      </UtilsButton>
    </div>
  </div>
</template>
