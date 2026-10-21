<script setup lang="ts">
import { EllipsisHorizontalIcon } from "@heroicons/vue/24/outline";
import { onClickOutside } from "@vueuse/core";
import type { ProjectRecord } from "~/utils/pengolo-api";

defineProps<{ project: ProjectRecord; editable: boolean }>();
const emit = defineEmits<{ edit: []; toggle: [event: Event] }>();
const menu = ref(false);
const container = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
function toggleMenu(event: MouseEvent) {
  if (event.currentTarget instanceof HTMLButtonElement)
    trigger.value = event.currentTarget;
  menu.value = !menu.value;
}
onClickOutside(container, () => {
  menu.value = false;
});
function close() {
  menu.value = false;
  trigger.value?.focus();
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
    <UiIconButton
      class="p-project-options"
      v-if="editable"
      variant="ghost"
      type="button"
      :aria-expanded="menu"
      aria-haspopup="menu"
      @click="toggleMenu"
      :icon="EllipsisHorizontalIcon"
      :label="
        $t('pengolo.copy.options_for_project_value0', {
          value0: project.data.name,
        })
      "
    />
    <div
      v-if="menu && editable"
      class="p-menu p-project-menu"
      role="menu"
      :aria-label="
        $t('pengolo.copy.manage_value0', { value0: project.data.name })
      "
    >
      <UiButton
        variant="ghost"
        type="button"
        role="menuitem"
        align="start"
        class="w-full"
        @click="
          close();
          emit('edit');
        "
      >
        {{ $t("pengolo.copy.rename_and_permissions") }}
      </UiButton>
    </div>
  </div>
</template>
