<script setup lang="ts">
import { PlusIcon, TrashIcon } from "@heroicons/vue/24/outline";
import {
  businessFields,
  conditionOperators,
  readConditions,
} from "~/utils/pipeline-conditions";
import type { BusinessCondition } from "~/utils/pipeline-conditions";
import type { NodeConfig } from "~/utils/pipeline";
const config = defineModel<NodeConfig>({ required: true });
const props = defineProps<{ mode: "route" | "filter" }>();
const { t } = useI18n();
const rows = ref<BusinessCondition[]>(
  readConditions(config.value.params?.conditions),
);
if (!rows.value.length)
  rows.value.push({ field: "subject", operator: "contains", value: "" });
function sync() {
  config.value = {
    ...config.value,
    ...(props.mode === "route"
      ? { routing: "conditions" as const, branch_labels: ["match", "other"] }
      : {}),
    params: {
      mode: props.mode,
      conditions: rows.value.map((row) => ({ ...row })),
      match_branch: "match",
      default_branch: "other",
    },
  };
}
function update(
  index: number,
  part: "field" | "operator" | "value",
  value: string | undefined,
) {
  if (value === undefined) return;
  const row = rows.value[index];
  if (!row) return;
  if (part === "field") {
    const field = businessFields.find((field) => field === value);
    if (field) row.field = field;
  } else if (part === "operator") {
    const operator = conditionOperators.find((operator) => operator === value);
    if (operator) row.operator = operator;
  } else row.value = value;
  sync();
}
onMounted(sync);
</script>
<template>
  <div class="space-y-3">
    <p class="text-sm text-muted">{{ $t("pipeline.conditions.all") }}</p>
    <fieldset
      v-for="(row, index) in rows"
      :key="index"
      class="space-y-2 border-b border-border pb-3"
    >
      <legend class="text-xs text-muted">
        {{ $t("pipeline.conditions.rule", { count: index + 1 }) }}
      </legend>
      <UiSelect
        :model-value="row.field"
        :label="$t('pipeline.conditions.field')"
        :options="
          businessFields.map((value) => ({
            value,
            label: t(`pipeline.conditions.fields.${value}`),
          }))
        "
        @update:model-value="update(index, 'field', $event)"
      />
      <UiSelect
        :model-value="row.operator"
        :label="$t('pipeline.conditions.operator')"
        :options="
          conditionOperators.map((value) => ({
            value,
            label: t(`pipeline.conditions.operators.${value}`),
          }))
        "
        @update:model-value="update(index, 'operator', $event)"
      />
      <div class="flex items-end gap-2">
        <UiInput
          :model-value="row.value"
          :label="$t('pipeline.conditions.value')"
          class="min-w-0 flex-1"
          maxlength="2048"
          @update:model-value="update(index, 'value', $event ?? '')"
        /><UiIconButton
          :icon="TrashIcon"
          :label="$t('ui.actions.delete')"
          :disabled="rows.length === 1"
          @click="
            rows.splice(index, 1);
            sync();
          "
        />
      </div>
    </fieldset>
    <UiButton
      variant="secondary"
      :disabled="rows.length >= 16"
      @click="
        rows.push({ field: 'subject', operator: 'contains', value: '' });
        sync();
      "
      ><template #leading><PlusIcon class="h-4 w-4" /></template
      >{{ $t("pipeline.conditions.add") }}</UiButton
    >
  </div>
</template>
