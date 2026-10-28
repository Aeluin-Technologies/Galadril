<script setup lang="ts">
import type { ReviewConflict } from "~/utils/pipeline-review";
defineProps<{ conflicts: readonly ReviewConflict[] }>();
const choices = defineModel<Record<string, "target" | "source">>({
  required: true,
});
const emit = defineEmits<{ change: [] }>();
function choose(path: string, value: string | undefined) {
  if (value !== "target" && value !== "source") return;
  choices.value = { ...choices.value, [path]: value };
  emit("change");
}
</script>
<template>
  <div class="space-y-4">
    <fieldset
      v-for="conflict in conflicts"
      :key="conflict.path"
      class="min-w-0 space-y-3 border-b border-border pb-4"
    >
      <legend class="break-all text-sm font-medium">{{ conflict.path }}</legend>
      <div class="grid min-w-0 gap-3 sm:grid-cols-2">
        <div class="min-w-0">
          <p class="text-xs text-muted">
            {{ $t("pipeline.collaboration.target") }}
          </p>
          <pre
            class="max-h-32 overflow-auto whitespace-pre-wrap break-all text-xs"
            >{{
              conflict.target === undefined || conflict.target === null
                ? $t("pipeline.collaboration.removed")
                : JSON.stringify(conflict.target, null, 2)
            }}</pre>
        </div>
        <div class="min-w-0">
          <p class="text-xs text-muted">
            {{ $t("pipeline.collaboration.source") }}
          </p>
          <pre
            class="max-h-32 overflow-auto whitespace-pre-wrap break-all text-xs"
            >{{
              conflict.source === undefined || conflict.source === null
                ? $t("pipeline.collaboration.removed")
                : JSON.stringify(conflict.source, null, 2)
            }}</pre>
        </div>
      </div>
      <UiSelect
        :model-value="choices[conflict.path] ?? ''"
        :label="$t('pipeline.collaboration.resolution')"
        :options="[
          { value: 'target', label: $t('pipeline.collaboration.keepTarget') },
          { value: 'source', label: $t('pipeline.collaboration.keepSource') },
        ]"
        @update:model-value="choose(conflict.path, $event)"
      />
    </fieldset>
  </div>
</template>
