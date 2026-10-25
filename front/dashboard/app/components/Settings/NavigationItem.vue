<script setup lang="ts">
import type { SettingsSection } from "~/types/settings";
defineProps<{ section: SettingsSection; active: boolean; to: string }>();
</script>

<template>
  <UiNavigationLink :active="active" variant="section">
    <NuxtLink :to="to" :aria-current="active ? 'page' : undefined">
      <span class="font-medium">{{ section.label }}</span>
      <span class="mt-1 hidden text-xs text-muted lg:block">{{
        section.description
      }}</span>
    </NuxtLink>
  </UiNavigationLink>
</template>
