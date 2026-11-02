<script setup lang="ts">
import { reviewChanges } from "~/utils/pipeline-review";
import type { Definition } from "~/utils/pipeline";
const { t, te } = useI18n();
const props = defineProps<{ before: Definition; after: Definition }>();
const changes = computed(() => reviewChanges(props.before, props.after));
const reviewed = ref<string[]>([]);
watch(
  () => [props.before, props.after],
  () => {
    reviewed.value = [];
  },
);
function mark(id: string, value: boolean | undefined) {
  reviewed.value = value
    ? [...reviewed.value, id]
    : reviewed.value.filter((item) => item !== id);
}
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function display(value: unknown): string {
  if (value === undefined || value === null) return "—";
  if (Array.isArray(value)) return value.map(display).join(", ");
  if (object(value)) {
    if (typeof value.field === "string" && typeof value.operator === "string")
      return `${value.field} ${value.operator.replaceAll("_", " ")} ${display(value.value)}`;
    return t("pipeline.review.nestedSettings", {
      count: Object.keys(value).length,
    });
  }
  return String(value);
}
function fields(value: unknown) {
  if (!object(value))
    return [
      {
        key: "value",
        label: t("pipeline.review.value"),
        value: display(value),
      },
    ];
  return Object.entries(value)
    .flatMap(([key, item]) =>
      key === "params" && object(item) ? Object.entries(item) : [[key, item]],
    )
    .map(([key, item]) => {
      const name = String(key);
      const translation = `pipeline.review.fields.${name}`;
      return {
        key: name,
        label: te(translation) ? t(translation) : name.replaceAll("_", " "),
        value: display(item),
      };
    });
}
</script>
<template>
  <section
    :aria-label="$t('pipeline.collaboration.comparison')"
    class="space-y-3"
  >
    <p class="text-sm text-muted" role="status">
      {{
        $t("pipeline.review.progress", {
          reviewed: reviewed.length,
          count: changes.length,
        })
      }}
    </p>
    <p v-if="!changes.length" class="text-sm text-muted">
      {{ $t("pipeline.collaboration.noChanges") }}
    </p>
    <details
      v-for="change in changes"
      :key="change.id"
      open
      class="rounded-control border border-border bg-surface"
    >
      <summary class="cursor-pointer px-4 py-3 text-sm font-medium">
        {{ change.label }}
        <UiBadge
          class="ms-2"
          :tone="
            change.kind === 'removed'
              ? 'danger'
              : change.kind === 'added'
                ? 'success'
                : 'neutral'
          "
          >{{ $t(`pipeline.review.${change.kind}`) }}</UiBadge
        >
      </summary>
      <div class="grid border-t border-border md:grid-cols-2">
        <div class="min-w-0 bg-surface-muted p-3">
          <h3 class="mb-2 text-xs font-semibold text-muted">
            {{ $t("pipeline.review.before") }}
          </h3>
          <dl class="space-y-2 text-sm">
            <div v-for="field in fields(change.before)" :key="field.key">
              <dt class="text-xs text-muted">{{ field.label }}</dt>
              <dd class="whitespace-pre-wrap break-words">{{ field.value }}</dd>
            </div>
          </dl>
        </div>
        <div class="min-w-0 p-3">
          <h3 class="mb-2 text-xs font-semibold text-muted">
            {{ $t("pipeline.review.after") }}
          </h3>
          <dl class="space-y-2 text-sm">
            <div v-for="field in fields(change.after)" :key="field.key">
              <dt class="text-xs text-muted">{{ field.label }}</dt>
              <dd class="whitespace-pre-wrap break-words">{{ field.value }}</dd>
            </div>
          </dl>
        </div>
      </div>
      <details class="border-t border-border p-3 text-xs">
        <summary class="cursor-pointer text-muted">
          {{ $t("pipeline.review.advanced") }}
        </summary>
        <pre
          class="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words"
          >{{
            JSON.stringify(
              { before: change.before, after: change.after },
              null,
              2,
            )
          }}</pre>
      </details>
      <UiCheckbox
        :model-value="reviewed.includes(change.id)"
        :label="$t('pipeline.review.markReviewed', { name: change.label })"
        class="m-3"
        @update:model-value="mark(change.id, $event)"
      />
    </details>
  </section>
</template>
