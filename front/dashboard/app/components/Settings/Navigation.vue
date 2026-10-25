<script setup lang="ts">
import type { SettingsSection } from "~/types/settings";
import SettingsNavigationItem from "./NavigationItem.vue";
defineProps<{
  label: string;
  sections: readonly SettingsSection[];
  active: string;
  base: string;
}>();
</script>

<template>
  <nav
    :aria-label="label"
    class="flex gap-2 overflow-x-auto pb-2 lg:sticky lg:top-8 lg:w-52 lg:shrink-0 lg:flex-col lg:self-start"
  >
    <SettingsNavigationItem
      v-for="section in sections"
      :key="section.id"
      :section="section"
      :active="active === section.id"
      :to="`${base}#${section.id}`"
    />
  </nav>
</template>
