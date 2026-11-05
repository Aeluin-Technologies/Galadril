<script setup lang="ts">
import { useIntersectionObserver } from "@vueuse/core";
import {
  pengoloOperation,
  pengoloRequest,
  type GenerationReplay,
  type PengoloEvent,
} from "~/utils/pengolo-api";
const props = withDefaults(
  defineProps<{
    generationId: string;
    temporary: boolean;
    hasCalculations?: boolean;
    inlineQuestions?: boolean;
    hasOtherActivity?: boolean;
  }>(),
  { hasOtherActivity: true },
);
const { t } = useI18n();
const events = ref<PengoloEvent[]>([]);
const status = ref("completed");
const loading = ref(false);
const loaded = ref(false);
const error = ref("");
const questionTarget = ref<HTMLElement | null>(null);
let request: AbortController | undefined;
function cancel() {
  const current = request;
  request = undefined;
  current?.abort();
  loading.value = false;
}
onDeactivated(cancel);
onBeforeUnmount(cancel);
useIntersectionObserver(questionTarget, ([entry]) => {
  if (entry?.isIntersecting && !error.value) void load();
});
function toggle(event: Event) {
  if (event.target instanceof HTMLDetailsElement && event.target.open)
    void load();
}
async function load() {
  if (loading.value || loaded.value) return;
  const current = new AbortController();
  request = current;
  loading.value = true;
  error.value = "";
  try {
    const collected: PengoloEvent[] = [];
    let after = 0;
    for (let pageNumber = 0; pageNumber < 256; pageNumber++) {
      const page = await pengoloRequest<GenerationReplay>(
        pengoloOperation("Events"),
        { id: props.generationId, after, temporary: props.temporary },
        current.signal,
      );
      if (request !== current) return;
      status.value = page.status;
      collected.push(...page.events.filter((item) => item.kind === "activity"));
      const cursor = page.events.at(-1)?.sequence ?? after;
      if (cursor <= after || page.events.length < 64) {
        loaded.value = true;
        break;
      }
      after = cursor;
    }
    events.value = collected;
    if (!loaded.value)
      error.value = "The activity history is too large to display completely.";
  } catch {
    if (request === current)
      error.value =
        "Activity could not be loaded. Check your access and retry.";
  } finally {
    if (request === current) {
      request = undefined;
      loading.value = false;
    }
  }
}
async function closedQuestion(): Promise<void> {
  throw new Error("This question is closed");
}
</script>
<template>
  <div v-if="inlineQuestions" ref="questionTarget" class="min-h-px">
    <AiRunActivity
      v-if="events.length"
      :events="events"
      :status="status"
      :can-answer="false"
      :submit="closedQuestion"
      mode="questions"
    />
    <UiButton v-if="error" variant="ghost" size="sm" :action="load">
      {{ $t("pengolo.questions.retryHistory") }}
    </UiButton>
  </div>
  <details v-if="hasOtherActivity" class="p-execution-history" @toggle="toggle">
    <summary>
      {{
        hasCalculations
          ? t("pengolo.viewCalculations")
          : t("pengolo.viewActivity")
      }}
    </summary>
    <p v-if="loading" role="status">Loading activity…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <AiRunActivity
      v-if="events.length"
      :events="events"
      :status="status"
      :can-answer="false"
      :submit="closedQuestion"
      :mode="inlineQuestions ? 'tools' : 'all'"
    />
    <p v-else-if="loaded">No recorded activity.</p>
  </details>
</template>
