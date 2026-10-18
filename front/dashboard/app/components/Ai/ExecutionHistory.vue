<script setup lang="ts">
import {
  pengoloOperation,
  pengoloRequest,
  type GenerationReplay,
  type PengoloEvent,
} from "~/utils/pengolo-api";
const props = defineProps<{ generationId: string; temporary: boolean }>();
const events = ref<PengoloEvent[]>([]);
const status = ref("completed");
const loading = ref(false);
const loaded = ref(false);
const error = ref("");
let epoch = 0;
onBeforeUnmount(() => {
  epoch++;
});
async function load(event: Event) {
  if (
    !(event.target instanceof HTMLDetailsElement) ||
    !event.target.open ||
    loading.value ||
    loaded.value
  )
    return;
  const current = epoch;
  loading.value = true;
  error.value = "";
  try {
    const collected: PengoloEvent[] = [];
    let after = 0;
    for (let pageNumber = 0; pageNumber < 256; pageNumber++) {
      const page = await pengoloRequest<GenerationReplay>(
        pengoloOperation("Events"),
        { id: props.generationId, after, temporary: props.temporary },
      );
      if (current !== epoch) return;
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
    if (current === epoch)
      error.value =
        "Activity could not be loaded. Check your access and retry.";
  } finally {
    if (current === epoch) loading.value = false;
  }
}
async function closedQuestion(): Promise<void> {
  throw new Error("This question is closed");
}
</script>
<template>
  <details class="p-execution-history" @toggle="load">
    <summary>View work and calculations</summary>
    <p v-if="loading" role="status">Loading activity…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <AiRunActivity
      v-if="events.length"
      :events="events"
      :status="status"
      :can-answer="false"
      :submit="closedQuestion"
    />
    <p v-else-if="loaded">No recorded activity.</p>
  </details>
</template>
