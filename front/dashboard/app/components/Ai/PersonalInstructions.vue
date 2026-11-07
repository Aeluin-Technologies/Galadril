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
const profileFields = ["firstName", "lastName", "address", "title", "email", "capacity"] as const;
const profile = reactive({
  firstName: "", lastName: "", address: "", title: "", email: "", capacity: "",
});
const profileError = ref("");
const profileNotice = ref("");
function clearProfile(): void {
  for (const field of profileFields) profile[field] = "";
}
async function saveProfile(): Promise<void> {
  if (!saved.value || busy.value) return;
  stop();
  const controller = new AbortController();
  pending = controller;
  busy.value = true;
  profileError.value = "";
  profileNotice.value = "";
  try {
    await pengoloMutation<boolean>(
      "savePengoloDocumentProfile",
      { revision: saved.value.revision, ...profile },
      controller.signal,
    );
    if (controller.signal.aborted) return;
    clearProfile();
    saved.value = { ...saved.value, revision: saved.value.revision + 1 };
    profileNotice.value = t("personalization.documentProfile.saved");
  } catch {
    if (!controller.signal.aborted)
      profileError.value = t("personalization.documentProfile.error");
  } finally {
    if (pending === controller) {
      pending = undefined;
      busy.value = false;
    }
  }
}
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
  profileError.value = "";
  profileNotice.value = "";
  clearProfile();
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
  <form
    class="mt-5 space-y-4 rounded-panel border border-border bg-surface p-6"
    autocomplete="off"
    @submit.prevent="saveProfile"
  >
    <h2 class="text-lg font-semibold">{{ $t("personalization.documentProfile.title") }}</h2>
    <p class="text-sm text-muted">{{ $t("personalization.documentProfile.help") }}</p>
    <div class="grid gap-4 sm:grid-cols-2">
      <UiInput
        v-for="field in profileFields"
        :key="field"
        v-model="profile[field]"
        :label="$t(`personalization.documentProfile.fields.${field}`)"
        :required="field === 'firstName' || field === 'lastName' || field === 'address'"
        :disabled="!saved || busy"
        :type="field === 'email' ? 'email' : 'text'"
        maxlength="256"
        autocomplete="off"
      />
    </div>
    <UiAlert v-if="profileError" tone="danger">{{ profileError }}</UiAlert>
    <p v-if="profileNotice" role="status" class="text-success">{{ profileNotice }}</p>
    <UiButton type="submit" :disabled="!saved || busy">{{
      $t("personalization.documentProfile.save")
    }}</UiButton>
  </form>
</template>
