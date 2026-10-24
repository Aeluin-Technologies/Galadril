<script setup lang="ts">
import type { EmailAccount, EmailProfile } from "~/utils/email";
import { parseProfile, can } from "~/utils/email";
const props = defineProps<{ account: EmailAccount }>();
const api = useEmailApi();
const catalog = usePipelineCatalog();
const { t } = useI18n();
const profile = ref<EmailProfile>({
  model: "",
  fallbacks: [],
  prompt:
    "Write a helpful, accurate reply. Do not invent facts or commitments.",
  skill_ids: ["utils.skill.email"],
  language: "English",
  tone: "Professional",
  signature: "",
  mode: "draft",
  allowed_senders: [],
  daily_limit: 10,
  sender_daily_limit: null,
  one_reply_per_thread: false,
});
const models = ref<Array<{ value: string; label: string }>>([]);
const fallbacks = ref(""),
  skills = ref("utils.skill.email"),
  senders = ref(""),
  limit = ref("10"),
  senderLimit = ref(""),
  error = ref(""),
  success = ref(false),
  loading = ref(true),
  saving = ref(false);
let epoch = 0;
async function load() {
  const own = ++epoch;
  loading.value = true;
  error.value = "";
  try {
    const [saved, available] = await Promise.all([
      api.operation({ operation: "profile", account: props.account.id }),
      catalog.models(),
    ]);
    if (own !== epoch) return;
    const parsed = parseProfile(saved);
    models.value = available.map((model) => ({
      value: model.alias,
      label: model.alias,
    }));
    if (parsed) profile.value = parsed;
    else profile.value.model = available[0]?.alias ?? "";
    fallbacks.value = profile.value.fallbacks.join(", ");
    skills.value = profile.value.skill_ids.join(", ");
    senders.value = profile.value.allowed_senders.join(", ");
    limit.value = String(profile.value.daily_limit);
    senderLimit.value =
      profile.value.sender_daily_limit === null
        ? ""
        : String(profile.value.sender_daily_limit);
  } catch {
    if (own === epoch) error.value = t("email.loadError");
  } finally {
    if (own === epoch) loading.value = false;
  }
}
watch(() => props.account.id, load, { immediate: true });
onDeactivated(() => {
  epoch++;
  api.cancel();
  loading.value = false;
});
onActivated(() => {
  if (!loading.value) load();
});
async function save() {
  if (saving.value) return;
  saving.value = true;
  error.value = "";
  success.value = false;
  try {
    const split = (value: string) =>
      value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
    profile.value.fallbacks = split(fallbacks.value);
    profile.value.skill_ids = split(skills.value);
    profile.value.allowed_senders = split(senders.value);
    profile.value.daily_limit = Number(limit.value);
    profile.value.sender_daily_limit = String(senderLimit.value).trim()
      ? Number(senderLimit.value)
      : null;
    await api.operation({
      operation: "save_profile",
      account: props.account.id,
      profile: profile.value,
    });
    success.value = true;
  } catch {
    error.value = t("email.saveError");
  } finally {
    saving.value = false;
  }
}
</script>
<template>
  <UiCard
    :title="$t('email.responseProfile')"
    :description="$t('email.profileHelp')"
  >
    <UiAlert v-if="error" tone="danger"
      >{{ error
      }}<UiButton variant="ghost" :action="load">{{
        $t("email.retry")
      }}</UiButton></UiAlert
    >
    <UiAlert v-if="success" tone="success">{{ $t("email.saved") }}</UiAlert>
    <p v-if="loading" role="status" class="text-sm text-muted">
      {{ $t("email.loading") }}
    </p>
    <form v-else class="mt-4 grid gap-4 sm:grid-cols-2" @submit.prevent="save">
      <UiSelect
        v-model="profile.model"
        :label="$t('email.model')"
        :options="models"
        :disabled="!can(account, 'edit')"
      />
      <UiInput
        v-model="fallbacks"
        :label="$t('email.fallbacks')"
        :help="$t('email.fallbackHelp')"
        :disabled="!can(account, 'edit')"
      />
      <UiInput
        v-model="skills"
        :label="$t('email.skills')"
        :help="$t('email.skillsHelp')"
        class="sm:col-span-2"
        :disabled="!can(account, 'edit')"
      />
      <UiTextarea
        v-model="profile.prompt"
        :label="$t('email.prompt')"
        class="sm:col-span-2"
        :disabled="!can(account, 'edit')"
      />
      <UiInput
        v-model="profile.language"
        :label="$t('email.language')"
        :disabled="!can(account, 'edit')"
      />
      <UiInput
        v-model="profile.tone"
        :label="$t('email.tone')"
        :disabled="!can(account, 'edit')"
      />
      <UiInput
        v-model="profile.signature"
        :label="$t('email.signature')"
        class="sm:col-span-2"
        :disabled="!can(account, 'edit')"
      />
      <UiSelect
        v-model="profile.mode"
        :label="$t('email.executionMode')"
        :options="[
          { value: 'draft', label: $t('email.modeDraft') },
          { value: 'approval', label: $t('email.modeApproval') },
          ...(can(account, 'automate')
            ? [{ value: 'automatic', label: $t('email.modeAutomatic') }]
            : []),
        ]"
        :disabled="!can(account, 'edit')"
      />
      <template v-if="profile.mode === 'automatic'">
        <UiInput
          v-model="senders"
          :label="$t('email.allowedSenders')"
          :help="$t('email.allowedSendersHelp')"
          :disabled="!can(account, 'automate')"
        />
        <UiInput
          v-model="limit"
          type="number"
          min="1"
          max="100"
          :label="$t('email.dailyLimit')"
          :disabled="!can(account, 'automate')"
        />
        <UiInput
          v-model="senderLimit"
          type="number"
          min="1"
          max="100"
          :label="$t('email.senderLimit')"
          :help="$t('email.senderLimitHelp')"
          :disabled="!can(account, 'automate')"
        />
        <UiCheckbox
          v-model="profile.one_reply_per_thread"
          :label="$t('email.onePerThread')"
          :help="$t('email.onePerThreadHelp')"
          :disabled="!can(account, 'automate')"
        />
        <UiAlert tone="warning" class="sm:col-span-2">{{
          $t("email.automaticWarning")
        }}</UiAlert>
      </template>
      <div v-if="can(account, 'edit')" class="sm:col-span-2">
        <UiButton
          type="submit"
          :loading="saving"
          :disabled="!profile.model"
          :pending-label="$t('email.saving')"
          >{{ $t("email.saveProfile") }}</UiButton
        >
      </div>
    </form>
  </UiCard>
</template>
