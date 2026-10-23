<script setup lang="ts">
import LegacyButton from "../../../../dashboard/app/components/Utils/Button.vue";
import LegacyModal from "../../../../dashboard/app/components/Utils/Modal.vue";
const open = ref(false);
const count = ref(0);
</script>

<template>
  <main class="space-y-4 p-8">
    <h1>Legacy adapter contracts</h1>
    <LegacyButton
      variant="secondary"
      type="button"
      tooltip="Open the workspace editor"
      aria-label="Edit workspace"
      @click="open = true"
      >Edit</LegacyButton
    >
    <LegacyButton disabled @click="count++">Unavailable</LegacyButton>
    <LegacyButton :tooltip="false" data-testid="plain-button" @click="count++"
      >Count</LegacyButton
    >
    <output aria-label="Click count">{{ count }}</output>
    <LegacyModal :is-open="open" title="Workspace editor" @close="open = false"
      ><p>Existing modal content</p>
      <template #footer
        ><LegacyButton @click="open = false"
          >Finish editing</LegacyButton
        ></template
      ></LegacyModal
    >
  </main>
</template>
