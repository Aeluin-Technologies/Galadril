<script setup lang="ts">
const quantity = ref(2);
const shared = ref(false);
const notes = ref("");
const approved = ref<string[]>([]);
</script>

<template>
  <main class="space-y-4 p-6">
    <h1>Dashboard form contracts</h1>
    <form>
      <label for="quantity">Quantity</label>
      <UiInput
        id="quantity"
        v-model.number="quantity"
        type="number"
        min="0"
        name="quantity"
      />
      <UiSelect
        v-model="shared"
        label="Access"
        name="access"
        :options="[
          { value: false, label: 'Personal' },
          { value: true, label: 'Shared' },
        ]"
      />
      <UiTextarea v-model="notes" label="Instructions" name="notes" />
      <UiCheckbox v-model="approved" value="analyst" label="Analyst" />
      <UiCheckbox v-model="approved" value="operator" label="Operator" />
      <output aria-label="Quantity value">{{ quantity + 1 }}</output>
      <output aria-label="Access value">{{
        shared ? "shared" : "personal"
      }}</output>
      <output aria-label="Selected roles">{{ approved.join(",") }}</output>
    </form>
  </main>
</template>
