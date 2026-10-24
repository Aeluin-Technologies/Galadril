<script setup lang="ts">
const open = ref(false);
const choice = ref("");
const menuChoice = ref("");
const databaseLabel = ref("Database");
</script>

<template>
  <main class="space-y-6 p-6">
    <h1>Overlay interaction contracts</h1>
    <UiMenu
      label="Add a block"
      :items="[
        { id: 'input', label: 'Input', description: 'Receive new documents' },
        { id: 'route', label: 'Route content', description: 'Choose a branch' },
        { id: 'disabled', label: 'Unavailable pipeline', disabled: true },
      ]"
      @select="menuChoice = $event"
    />
    <p role="status" aria-label="Menu selection">{{ menuChoice }}</p>
    <UiButton @click="open = true">Choose a connection</UiButton>
    <UiDialog v-model:open="open" title="Connection settings">
      <form>
        <UiSelect
          v-model="choice"
          name="connection"
          label="Connection type"
          placeholder="Choose a type"
          help="Choose an available connector."
          required
          :options="[
            { value: 'database', label: databaseLabel },
            { value: 'stream', label: 'Event stream', disabled: true },
            { value: 'storage', label: 'Object storage' },
          ]"
        />
        <UiButton @click="databaseLabel = 'Analytics database'"
          >Rename connection</UiButton
        >
      </form>
      <template #footer
        ><UiButton @click="open = false">Done</UiButton></template
      >
    </UiDialog>
  </main>
</template>
