<script setup lang="ts">
import { parse, stringify } from "yaml";
import { parseSemanticDocument } from "~/utils/ontology";
import { ontologyCodeLines } from "~/utils/ontology-code";
import type { SemanticDocument } from "~/types/ontology";
const props = defineProps<{
  document: SemanticDocument;
  editable: boolean;
  selected: string;
  active: boolean;
}>();
const emit = defineEmits<{ apply: [document: SemanticDocument] }>();
const source = computed(() => stringify(props.document));
const draft = ref(source.value);
const invalid = ref(false);
const panel = ref<HTMLElement | null>(null);
const lines = computed(() => ontologyCodeLines(props.document, props.selected));
watch(
  [() => props.active, () => props.selected, () => props.editable],
  async () => {
    await nextTick();
    if (!props.active || !panel.value) return;
    const first = panel.value.querySelector<HTMLElement>(
      "[data-ontology-highlight]",
    );
    if (first)
      panel.value.scrollTop = Math.max(
        0,
        first.offsetTop - panel.value.clientHeight / 3,
      );
  },
);
watch(source, (value) => {
  draft.value = value;
  invalid.value = false;
});
function apply() {
  if (!props.editable) return;
  try {
    if (draft.value.length > 524288) throw new Error("ONTOLOGY_CODE_TOO_LARGE");
    const value: unknown = parse(draft.value, { maxAliasCount: 0 });
    const result = parseSemanticDocument(value);
    if (result.plugin_origin || result.target_origin)
      throw new Error("ONTOLOGY_ORIGIN_IMMUTABLE");
    emit("apply", result);
    invalid.value = false;
  } catch {
    invalid.value = true;
  }
}
</script>
<template>
  <section
    class="space-y-3 rounded-panel border border-border bg-surface p-4"
    :aria-label="$t('ontologyManager.yamlCode')"
  >
    <UiField :label="$t('ontologyManager.yamlCode')" v-slot="field"
      ><UiTextarea
        v-if="editable"
        :id="field.id"
        v-model="draft"
        class="min-h-[420px] font-mono text-xs"
        spellcheck="false"
      />
      <pre
        v-else
        ref="panel"
        class="relative max-h-[65vh] overflow-auto whitespace-pre text-xs"
      ><code><span v-for="line in lines" :key="line.number" class="block min-h-4 border-l-2 px-3" :class="line.highlighted ? 'border-primary bg-primary-subtle font-semibold text-primary-foreground' : 'border-transparent'" :data-ontology-highlight="line.highlighted ? '' : undefined">{{ line.text || ' ' }}</span></code></pre>
    </UiField>
    <p v-if="invalid" role="alert" class="text-sm text-danger">
      {{ $t("ontologyManager.invalidCode") }}
    </p>
    <UiButton v-if="editable" variant="secondary" @click="apply">{{
      $t("ontologyManager.applyCode")
    }}</UiButton>
  </section>
</template>
