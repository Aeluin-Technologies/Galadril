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
  <UtilsModal
    :is-open="isOpen"
    :title="
      $t('builder.config_modal.title', { type: widget?.type || 'Widget' })
    "
    @close="emit('close')"
  >
    <div v-if="widget?.type === 'Card'" class="space-y-4">
      <div>
        <label class="block text-sm font-medium text-zinc-700 mb-1">{{
          $t("builder.config_modal.card.title")
        }}</label>
        <input
          v-model="formData.title"
          type="text"
          class="w-full border border-zinc-200 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-amber-500 focus:outline-none"
        />
      </div>
      <div>
        <label class="block text-sm font-medium text-zinc-700 mb-1">{{
          $t("builder.config_modal.card.value")
        }}</label>
        <input
          v-model="formData.value"
          type="text"
          class="w-full border border-zinc-200 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-amber-500 focus:outline-none"
        />
      </div>
      <div>
        <label class="block text-sm font-medium text-zinc-700 mb-1">{{
          $t("builder.config_modal.card.trend")
        }}</label>
        <input
          v-model="formData.trend"
          type="text"
          class="w-full border border-zinc-200 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-amber-500 focus:outline-none"
        />
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
        <label class="block text-sm font-medium text-zinc-700 mb-1">{{
          $t("builder.config_modal.generic.title")
        }}</label>
        <input
          v-model="formData.title"
          type="text"
          class="w-full border border-zinc-200 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-amber-500 focus:outline-none"
        />
      </div>
      <div v-if="widget?.type === 'LineChart'">
        <p class="text-xs text-zinc-500 mt-2">
          {{ $t("builder.config_modal.line_chart.info") }}
        </p>
      </div>
    </div>

    <div v-else-if="widget?.type === 'Map'" class="space-y-4">
      <p class="text-sm text-zinc-500">
        {{ $t("builder.config_modal.map.info") }}
      </p>
    </div>

    <template #footer>
      <button
        @click="emit('close')"
        class="px-4 py-2 text-sm font-medium text-zinc-600 hover:text-zinc-800"
      >
        {{ $t("builder.config_modal.buttons.cancel") }}
      </button>
      <button
        @click="handleSave"
        class="px-4 py-2 text-sm font-medium text-white bg-amber-600 rounded-lg hover:bg-amber-700 shadow-sm transition-colors"
      >
        {{ $t("builder.config_modal.buttons.save") }}
      </button>
    </template>
  </UtilsModal>
</template>
