<script setup lang="ts">
import type { OntologyTypeDraft } from "~/utils/ontology-edit";
const open = defineModel<boolean>("open", { required: true });
defineProps<{ error?: string }>();
const emit = defineEmits<{ create: [draft: OntologyTypeDraft] }>();
const step = ref(0),
  kind = ref("object_type");
const draft = reactive({ id: "", name: "", definition: "", example: "" });
const valid = computed(
  () =>
    !!draft.name.trim() &&
    !!draft.definition.trim() &&
    /^[a-z][a-z0-9_.-]{0,255}$/.test(draft.id),
);
watch(open, (value) => {
  if (!value) return;
  step.value = 0;
  kind.value = "object_type";
  Object.assign(draft, {
    id: `type-${crypto.randomUUID()}`,
    name: "",
    definition: "",
    example: "",
  });
});
function create() {
  if (!valid.value) return;
  emit("create", {
    ...draft,
    kind: kind.value === "event_type" ? "event_type" : "object_type",
  });
}
</script>
<template>
  <UiDialog
    v-model:open="open"
    :title="$t('ontologyManager.objectWizard.title')"
    :description="$t('ontologyManager.objectWizard.help')"
  >
    <ol
      class="mb-5 flex gap-4 text-sm"
      :aria-label="$t('ontologyManager.objectWizard.steps')"
    >
      <li
        v-for="(key, index) in ['identity', 'shape', 'review']"
        :key="key"
        :aria-current="step === index ? 'step' : undefined"
        :class="
          step === index
            ? 'font-semibold text-primary-foreground'
            : 'text-muted'
        "
      >
        {{ index + 1 }}. {{ $t(`ontologyManager.objectWizard.${key}`) }}
      </li>
    </ol>
    <div v-if="step === 0" class="space-y-4">
      <UiInput
        v-model="draft.name"
        :label="$t('ontologyManager.name')"
        maxlength="256"
        autofocus
      />
      <UiField
        :label="$t('ontologyManager.objectWizard.definition')"
        v-slot="field"
        ><UiTextarea :id="field.id" v-model="draft.definition" maxlength="4096"
      /></UiField>
      <UiInput
        v-model="draft.id"
        :label="$t('ontologyManager.identifier')"
        :help="$t('ontologyManager.objectWizard.identifierHelp')"
        maxlength="256"
      />
    </div>
    <div v-else-if="step === 1" class="space-y-4">
      <UiSelect
        v-model="kind"
        :label="$t('ontologyManager.type')"
        :options="[
          {
            value: 'object_type',
            label: $t('ontologyManager.objectWizard.object'),
          },
          {
            value: 'event_type',
            label: $t('ontologyManager.objectWizard.event'),
          },
        ]"
      />
      <UiField
        :label="$t('ontologyManager.objectWizard.example')"
        :help="$t('ontologyManager.objectWizard.exampleHelp')"
        v-slot="field"
        ><UiTextarea :id="field.id" v-model="draft.example" maxlength="4096"
      /></UiField>
    </div>
    <dl v-else class="space-y-4 text-sm">
      <div>
        <dt class="text-muted">{{ $t("ontologyManager.name") }}</dt>
        <dd class="font-semibold">{{ draft.name }}</dd>
      </div>
      <div>
        <dt class="text-muted">
          {{ $t("ontologyManager.objectWizard.definition") }}
        </dt>
        <dd class="whitespace-pre-wrap">{{ draft.definition }}</dd>
      </div>
      <div>
        <dt class="text-muted">{{ $t("ontologyManager.type") }}</dt>
        <dd>
          {{
            $t(
              kind === "event_type"
                ? "ontologyManager.objectWizard.event"
                : "ontologyManager.objectWizard.object",
            )
          }}
        </dd>
      </div>
      <div v-if="draft.example">
        <dt class="text-muted">
          {{ $t("ontologyManager.objectWizard.example") }}
        </dt>
        <dd class="whitespace-pre-wrap">{{ draft.example }}</dd>
      </div>
    </dl>
    <p v-if="error" role="alert" class="mt-4 text-sm text-danger">
      {{ error }}
    </p>
    <template #footer>
      <UiButton v-if="step > 0" variant="secondary" @click="step--">{{
        $t("ontologyManager.objectWizard.back")
      }}</UiButton>
      <UiButton v-if="step < 2" :disabled="!valid" @click="step++">{{
        $t("ontologyManager.objectWizard.next")
      }}</UiButton>
      <UiButton v-else :disabled="!valid" @click="create">{{
        $t("ontologyManager.objectWizard.create")
      }}</UiButton>
    </template>
  </UiDialog>
</template>
