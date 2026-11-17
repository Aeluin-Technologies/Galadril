<script setup lang="ts">
import type { GolSourceFile } from "~/types/ontology";

defineProps<{
  latest: readonly GolSourceFile[];
  draft: readonly GolSourceFile[];
}>();
const open = defineModel<boolean>("open", { required: true });
const emit = defineEmits<{ keep: [] }>();
</script>

<template>
  <UiDialog
    v-model:open="open"
    :title="$t('gol.reviewLatest')"
    :description="$t('gol.reviewLatestHelp')"
    size="wide"
  >
    <div class="grid gap-5 lg:grid-cols-2">
      <section :aria-label="$t('gol.latestSource')">
        <h3 class="mb-3 font-semibold">{{ $t("gol.latestSource") }}</h3>
        <article v-for="file in latest" :key="file.path" class="mb-4">
          <h4 class="break-all text-sm font-medium">{{ file.path }}</h4>
          <pre
            class="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs"
            >{{ file.text }}</pre>
        </article>
      </section>
      <section :aria-label="$t('gol.localSource')">
        <h3 class="mb-3 font-semibold">{{ $t("gol.localSource") }}</h3>
        <article v-for="file in draft" :key="file.path" class="mb-4">
          <h4 class="break-all text-sm font-medium">{{ file.path }}</h4>
          <pre
            class="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs"
            >{{ file.text }}</pre>
        </article>
      </section>
    </div>
    <template #footer>
      <UiButton variant="secondary" @click="open = false">{{
        $t("gol.cancel")
      }}</UiButton>
      <UiButton @click="emit('keep')">{{ $t("gol.keepDraft") }}</UiButton>
    </template>
  </UiDialog>
</template>
