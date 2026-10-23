<script setup>
const props = defineProps({
  isOpen: Boolean,
  widget: Object,
});

const emit = defineEmits(["close", "save"]);
const formData = ref({});

watch(
  () => props.widget,
  (newWidget) => {
    if (newWidget) {
      formData.value = JSON.parse(JSON.stringify(newWidget.config || {}));
      if (newWidget.config.icon) formData.value.icon = newWidget.config.icon;
    }
  },
  { immediate: true },
);

const handleSave = () => {
  emit("save", { ...props.widget, config: formData.value });
  emit("close");
};
</script>

<template>
  <UiDialog
    :open="isOpen"
    :title="
      $t('builder.config_modal.title', { type: widget?.type || 'Widget' })
    "
    @close="emit('close')"
  >
    <div v-if="widget?.type === 'Card'" class="space-y-4">
      <div>
        <label class="block text-sm font-medium text-foreground mb-1">{{
          $t("builder.config_modal.card.title")
        }}</label>
        <UiInput v-model="formData.title" type="text" class="w-full" />
      </div>
      <div>
        <label class="block text-sm font-medium text-foreground mb-1">{{
          $t("builder.config_modal.card.value")
        }}</label>
        <UiInput v-model="formData.value" type="text" class="w-full" />
      </div>
      <div>
        <label class="block text-sm font-medium text-foreground mb-1">{{
          $t("builder.config_modal.card.trend")
        }}</label>
        <UiInput v-model="formData.trend" type="text" class="w-full" />
      </div>
    </div>

    <div
      v-else-if="
        widget?.type === 'LineChart' ||
        widget?.type === 'Alerts' ||
        widget?.type === 'Objects'
      "
      class="space-y-4"
    >
      <div>
        <label class="block text-sm font-medium text-foreground mb-1">{{
          $t("builder.config_modal.generic.title")
        }}</label>
        <UiInput v-model="formData.title" type="text" class="w-full" />
      </div>
      <div v-if="widget?.type === 'LineChart'">
        <p class="text-xs text-muted mt-2">
          {{ $t("builder.config_modal.line_chart.info") }}
        </p>
      </div>
    </div>

    <div v-else-if="widget?.type === 'Map'" class="space-y-4">
      <p class="text-sm text-muted">
        {{ $t("builder.config_modal.map.info") }}
      </p>
    </div>

    <template #footer>
      <UiButton @click="emit('close')" variant="ghost">
        {{ $t("builder.config_modal.buttons.cancel") }}
      </UiButton>
      <UiButton @click="handleSave" variant="primary">
        {{ $t("builder.config_modal.buttons.save") }}
      </UiButton>
    </template>
  </UiDialog>
</template>
