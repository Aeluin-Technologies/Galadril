<script setup lang="ts">
import { PencilIcon } from "@heroicons/vue/24/outline";
import {
  pengoloMutation,
  pengoloOperation,
  pengoloRequest,
} from "~/utils/pengolo-api";

const fields = ["firstName", "lastName", "email", "address"] as const;
type Field = (typeof fields)[number];
type Profile = Record<Field, string> & {
  revision: number;
  requireMcpEmailReview: boolean;
};
const { t } = useI18n();
const { session } = useAccount();
const editForm = useId();
const profile = shallowRef<Profile | null>(null);
const review = ref(false);
const editing = ref<Field>();
const draft = ref("");
const busy = ref(false);
const error = ref("");
const notice = ref("");
let pending: AbortController | undefined;
let active = false;

function clear(): void {
  pending?.abort();
  pending = undefined;
  profile.value = null;
  review.value = false;
  editing.value = undefined;
  draft.value = "";
  busy.value = false;
  error.value = "";
  notice.value = "";
}

function invalidate(): void {
  clear();
  error.value = t("accountSettings.loadError");
}

async function request(updated?: Profile, savingReview = false): Promise<void> {
  if (busy.value) return;
  const controller = new AbortController();
  pending = controller;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    if (updated) {
      const saved = await pengoloMutation<boolean>(
        "savePengoloDocumentProfile",
        updated,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (!saved) throw new Error("Profile update was not confirmed");
      profile.value = { ...updated, revision: updated.revision + 1 };
      if (savingReview) {
        notice.value = t("accountSettings.emailApprovalSaved");
      } else {
        cancel();
        notice.value = t("accountSettings.saved");
      }
    } else {
      const result = await pengoloRequest<Profile>(
        pengoloOperation("PengoloDocumentProfile"),
        {},
        controller.signal,
      );
      if (!controller.signal.aborted) {
        profile.value = result;
        review.value = result.requireMcpEmailReview;
      }
    }
  } catch {
    if (!controller.signal.aborted)
      error.value = t(
        updated ? "accountSettings.saveError" : "accountSettings.loadError",
      );
  } finally {
    if (pending === controller) {
      pending = undefined;
      busy.value = false;
    }
  }
}

function edit(field: Field): void {
  if (!profile.value) return;
  draft.value = profile.value[field];
  editing.value = field;
  error.value = "";
  notice.value = "";
}

function cancel(): void {
  editing.value = undefined;
  draft.value = "";
  error.value = "";
}

function save(): Promise<void> | undefined {
  if (profile.value && editing.value)
    return request({ ...profile.value, [editing.value]: draft.value.trim() });
}

function saveReview(): Promise<void> | undefined {
  if (profile.value && editing.value === undefined)
    return request(
      { ...profile.value, requireMcpEmailReview: review.value },
      true,
    );
}

function activate(): void {
  active = true;
  if (!profile.value) void request();
}

watch(
  [() => session.value?.tenant, () => session.value?.subject],
  () => {
    clear();
    if (active && session.value) void request();
  },
  { flush: "sync" },
);
onMounted(() => {
  window.addEventListener("pengolo-access-invalidated", invalidate);
  activate();
});
onActivated(activate);
onDeactivated(() => {
  active = false;
  clear();
});
onBeforeUnmount(() => {
  active = false;
  clear();
  window.removeEventListener("pengolo-access-invalidated", invalidate);
});
</script>

<template>
  <section aria-labelledby="account-general" class="space-y-4">
    <h2 id="account-general" class="text-lg font-semibold">
      {{ $t("accountSettings.general") }}
    </h2>
    <div class="rounded-panel border border-border bg-surface px-4 sm:px-6">
      <p v-if="!profile && busy" role="status" class="py-4 text-sm text-muted">
        {{ $t("accountSettings.loading") }}
      </p>
      <dl v-if="profile" class="divide-y divide-border">
        <div
          v-for="field in fields"
          :key="field"
          class="grid gap-2 py-4 sm:grid-cols-[minmax(8rem,1fr)_minmax(0,2fr)] sm:items-center"
        >
          <dt class="font-medium">
            {{ $t(`accountSettings.fields.${field}`) }}
          </dt>
          <dd class="min-w-0">
            <div
              class="flex min-w-0 items-center justify-between gap-3 sm:justify-end"
            >
              <span
                class="break-words text-sm text-muted [overflow-wrap:anywhere]"
                >{{ profile[field] || $t("accountSettings.empty") }}</span
              >
              <UiIconButton
                :icon="PencilIcon"
                :label="
                  $t('accountSettings.edit', {
                    field: $t(`accountSettings.fields.${field}`),
                  })
                "
                :disabled="busy"
                variant="ghost"
                @click="edit(field)"
              />
            </div>
          </dd>
        </div>
      </dl>
    </div>
    <p class="text-sm text-muted">{{ $t("accountSettings.help") }}</p>
    <form
      v-if="profile"
      class="space-y-3 rounded-panel border border-border bg-surface px-4 py-4 sm:px-6"
      @submit.prevent="saveReview"
    >
      <UiCheckbox
        v-model="review"
        :label="$t('accountSettings.emailApproval')"
        :help="$t('accountSettings.emailApprovalHelp')"
        :disabled="busy || editing !== undefined"
      />
      <div
        v-if="review !== profile.requireMcpEmailReview"
        class="flex justify-end"
      >
        <UiButton
          type="submit"
          :loading="busy"
          :disabled="editing !== undefined"
        >
          {{ $t("accountSettings.saveEmailApproval") }}
        </UiButton>
      </div>
    </form>
    <UiDialog
      :open="editing !== undefined"
      :title="
        editing
          ? $t('accountSettings.edit', {
              field: $t(`accountSettings.fields.${editing}`),
            })
          : ''
      "
      :close-label="$t('actions.close')"
      :dismissible="!busy"
      @close="cancel"
    >
      <form
        v-if="editing"
        :id="editForm"
        class="space-y-3"
        @submit.prevent="save"
      >
        <UiInput
          v-model="draft"
          :label="$t(`accountSettings.fields.${editing}`)"
          :type="editing === 'email' ? 'email' : 'text'"
          :disabled="busy"
          maxlength="256"
          autocomplete="off"
          autofocus
        />
        <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
      </form>
      <template #footer>
        <UiButton variant="secondary" :disabled="busy" @click="cancel">
          {{ $t("actions.cancel") }}
        </UiButton>
        <UiButton type="submit" :form="editForm" :loading="busy">
          {{ $t("accountSettings.save") }}
        </UiButton>
      </template>
    </UiDialog>
    <UiAlert v-if="error && !editing" tone="danger">{{ error }}</UiAlert>
    <UiButton
      v-if="error && !editing"
      variant="secondary"
      :disabled="busy"
      @click="request()"
      >{{ $t("accountSettings.reload") }}</UiButton
    >
    <p v-if="notice" role="status" class="text-sm text-success">{{ notice }}</p>
  </section>
</template>
