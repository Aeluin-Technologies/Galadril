<script setup lang="ts">
import {
  pengoloMutation,
  pengoloOperation,
  pengoloRequest,
} from "~/utils/pengolo-api";

interface Preferences {
  revision: number;
  instructions: string;
}
const { t } = useI18n();
const { session } = useAccount();
let active = true;
const instructions = ref("");
const saved = ref<Preferences | null>(null);
const busy = ref(false);
const error = ref("");
const notice = ref("");
let pending: AbortController | undefined;
const dirty = computed(
  () => saved.value !== null && instructions.value !== saved.value.instructions,
);

/** Cancel stale responses when the settings panel leaves the active page. */
function stop(): void {
  pending?.abort();
  pending = undefined;
  busy.value = false;
}

/** A cached settings page must never retain another authenticated user's text. */
function clear(): void {
  stop();
  instructions.value = "";
  saved.value = null;
  error.value = "";
  notice.value = "";
}
function invalidate(): void {
  clear();
  error.value = t("personalization.loadError");
}
watch([() => session.value?.tenant, () => session.value?.subject], () => {
  clear();
  if (active) void request();
});

/** Preserve edits on failure; revisions prevent overwriting another tab. */
async function request(save = false): Promise<void> {
  if (busy.value) return;
  stop();
  const controller = new AbortController();
  pending = controller;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result =
      save && saved.value
        ? await pengoloMutation<Preferences>(
            "updatePengoloPreferences",
            {
              revision: saved.value.revision,
              instructions: instructions.value,
            },
            controller.signal,
          )
        : await pengoloRequest<Preferences>(
            pengoloOperation("PengoloPreferences"),
            {},
            controller.signal,
          );
    if (controller.signal.aborted) return;
    saved.value = result;
    instructions.value = result.instructions;
    if (save) notice.value = t("personalization.saved");
  } catch {
    if (!controller.signal.aborted)
      error.value = t(
        save ? "personalization.saveError" : "personalization.loadError",
      );
  } finally {
    if (pending === controller) {
      pending = undefined;
      busy.value = false;
    }
  }
}
onMounted(() => {
  window.addEventListener("pengolo-access-invalidated", invalidate);
  void request();
});
onBeforeUnmount(() => {
  clear();
  window.removeEventListener("pengolo-access-invalidated", invalidate);
});
onDeactivated(() => {
  active = false;
  clear();
});
onActivated(() => {
  active = true;
  if (!saved.value && !busy.value) void request();
});
</script>

<template>
  <UiPageHeader
    :title="$t('personalization.title')"
    :description="$t('personalization.description')"
  />
  <form
    class="space-y-5 rounded-panel border border-border bg-surface p-6"
    @submit.prevent="request(true)"
  >
    <p v-if="busy && !saved" role="status" class="text-muted">
      {{ $t("personalization.loading") }}
    </p>
    <UiTextarea
      v-model="instructions"
      :label="$t('personalization.label')"
      :help="$t('personalization.help')"
      :disabled="!saved || busy"
      maxlength="2000"
      rows="8"
    />
    <p class="text-sm text-muted">
      {{ $t("personalization.limit", { count: instructions.length }) }}
    </p>
    <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
    <p v-if="notice" role="status" class="text-success">{{ notice }}</p>
    <div class="flex flex-wrap gap-3">
      <UiButton type="submit" :loading="busy" :disabled="!dirty">{{
        $t("personalization.save")
      }}</UiButton>
      <UiButton
        v-if="error"
        type="button"
        variant="secondary"
        :disabled="busy"
        @click="request()"
        >{{ $t("personalization.reload") }}</UiButton
      >
    </div>
  </form>
</template>
