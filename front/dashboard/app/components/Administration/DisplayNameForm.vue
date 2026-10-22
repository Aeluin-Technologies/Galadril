<script setup lang="ts">
const props = defineProps<{ currentName: string; busy: boolean }>();
const emit = defineEmits<{ save: [name: string] }>();
const name = ref(props.currentName);
watch(
  () => props.currentName,
  (value) => {
    name.value = value;
  },
);
</script>

<template>
  <form class="space-y-4" @submit.prevent="emit('save', name.trim())">
    <UiInput
      v-model="name"
      :label="$t('administration.display_name')"
      :help="$t('administration.display_name_help')"
      maxlength="128"
      required
      :disabled="busy"
    />
    <UiButton
      type="submit"
      variant="secondary"
      :loading="busy"
      :disabled="!name.trim() || name.trim() === currentName"
      >{{ $t("administration.save_display_name") }}</UiButton
    >
  </form>
</template>
