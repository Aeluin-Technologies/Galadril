<script setup lang="ts">
import type { OntologyResource } from "~/types/ontology";
import type { OntologyLinkDraft } from "~/utils/ontology-edit";
const props = defineProps<{
  draft?: OntologyLinkDraft;
  types: OntologyResource[];
  error?: string;
}>();
const emit = defineEmits<{
  save: [draft: OntologyLinkDraft];
  remove: [id: string];
  close: [];
}>();
const open = ref(false);
const name = ref(""),
  source = ref(""),
  target = ref("");
watch(
  () => props.draft,
  (value) => {
    open.value = !!value;
    name.value = value?.name ?? "";
    source.value = value?.source ?? "";
    target.value = value?.target ?? "";
  },
  { immediate: true },
);
const options = computed(() =>
  props.types.map((type) => ({
    value: type.resource_id,
    label: type.display_name,
  })),
);
function save() {
  if (props.draft && name.value.trim() && source.value && target.value)
    emit("save", {
      ...props.draft,
      name: name.value,
      source: source.value,
      target: target.value,
    });
}
</script>
<template>
  <UiDialog
    v-model:open="open"
    :title="$t('ontologyManager.relationshipEditor.title')"
    :description="$t('ontologyManager.relationshipEditor.help')"
    @close="emit('close')"
  >
    <div class="space-y-4">
      <UiInput
        v-model="name"
        :label="$t('ontologyManager.name')"
        maxlength="256"
        autofocus
      />
      <UiSelect
        v-model="source"
        :label="$t('ontologyManager.relationshipEditor.source')"
        :options="options"
      />
      <UiSelect
        v-model="target"
        :label="$t('ontologyManager.relationshipEditor.target')"
        :options="options"
      />
      <p v-if="error" role="alert" class="text-sm text-danger">{{ error }}</p>
    </div>
    <template #footer>
      <UiButton
        v-if="draft?.existing"
        variant="danger"
        @click="emit('remove', draft.id)"
        >{{ $t("ontologyManager.relationshipEditor.remove") }}</UiButton
      >
      <UiButton :disabled="!name.trim() || !source || !target" @click="save">{{
        $t("ontologyManager.relationshipEditor.save")
      }}</UiButton>
    </template>
  </UiDialog>
</template>
