<script setup lang="ts">
import type { EmailAccount, EmailProfile } from "~/utils/email";
import { parseProfile, can, defaultEmailSignature } from "~/utils/email";
const props = defineProps<{ account: EmailAccount }>();
const api = useEmailApi();
const catalog = usePipelineCatalog();
const { t } = useI18n();
const profile = ref<EmailProfile>({
  track_opens: false,
  model: "",
  fallbacks: [],
  prompt:
    "Write a helpful, accurate reply. Do not invent facts or commitments.",
  skill_ids: ["utils.skill.email"],
  language: "English",
  tone: "Professional",
  signature: defaultEmailSignature,
  mode: "draft",
  allowed_senders: [],
  daily_limit: 10,
  sender_daily_limit: null,
  one_reply_per_thread: false,
});
const models = ref<Array<{ value: string; label: string }>>([]);
const bindingOpen = ref(false);
function bindPipeline(artifact: import("~/utils/pipeline").PipelineArtifact) {
  if (
    !artifact.definition.sources.some(
      (source) => source.capability_id === "utils.input.email",
    ) ||
    !artifact.definition.pipeline.some(
      (step) => step.capability_id === "utils.output.reply",
    )
  ) {
    error.value = t("email.pipeline.incompatible");
    return;
  }
  profile.value.pipeline = {
    pipeline_id: artifact.pipeline_id,
    revision_id: artifact.head_revision_id,
  };
  success.value = false;
}
const signatureField = ref("");
watch(signatureField, async (value) => {
  if (!value) return;
  profile.value.signature +=
    (profile.value.signature ? "\n" : "") + "{{ " + value + " }}";
  await nextTick();
  signatureField.value = "";
});
const skills = ref<{ value: string; label: string; description: string }[]>([]),
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
    const [saved, available, capabilities] = await Promise.all([
      api.operation({ operation: "profile", account: props.account.id }),
      catalog.models(),
      catalog.capabilities(),
    ]);
    if (own !== epoch) return;
    const parsed = parseProfile(saved);
    models.value = available.map((model) => ({
      value: model.alias,
      label: model.alias,
    }));
    if (parsed) profile.value = parsed;
    else profile.value.model = available[0]?.alias ?? "";
    skills.value = capabilities
      .filter(
        (item) =>
          item.kind.toLowerCase() === "skill" &&
          item.targets.some((target) => target.toLowerCase() === "scribe"),
      )
      .map((item) => ({
        value: item.id,
        label: item.name,
        description: item.description,
      }));
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
    profile.value.fallbacks = [];
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
      <UiSearchSelect
        :model-value="profile.skill_ids"
        @update:model-value="
          (value) => {
            if (Array.isArray(value)) profile.skill_ids = value;
          }
        "
        :label="$t('email.skills')"
        :search-label="$t('email.searchSkills')"
        :empty-label="$t('email.noMatches')"
        :options="skills"
        multiple
        :disabled="!can(account, 'edit')"
      />
      <UiTextarea
        v-model="profile.prompt"
        :label="$t('email.prompt')"
        class="sm:col-span-2"
        :disabled="!can(account, 'edit')"
      />
      <UiSearchSelect
        :model-value="profile.language"
        @update:model-value="
          (value) => {
            if (typeof value === 'string') profile.language = value;
          }
        "
        :label="$t('email.language')"
        :search-label="$t('email.searchLanguages')"
        :empty-label="$t('email.noMatches')"
        :options="[
          { value: 'French', label: 'Français' },
          { value: 'English', label: 'English' },
        ]"
        :disabled="!can(account, 'edit')"
      />
      <UiInput
        v-model="profile.tone"
        :label="$t('email.tone')"
        :disabled="!can(account, 'edit')"
      />
      <UiTextarea
        v-model="profile.signature"
        :label="$t('email.signature')"
        :help="$t('email.signatureHelp')"
        maxlength="8192"
        class="sm:col-span-2"
        :disabled="!can(account, 'edit')"
      />
      <EmailSignaturePreview :html="profile.signature" class="sm:col-span-2" />
      <EmailTrackingOption
        v-model="profile.track_opens"
        :disabled="!can(account, 'edit') || saving"
        class="sm:col-span-2"
      />
      <UiSelect
        v-model="signatureField"
        :label="$t('email.insertSignatureField')"
        :help="$t('email.signatureFieldsHelp')"
        :options="[
          { value: '', label: $t('email.chooseSignatureField') },
          ...['ref', 'subject', 'sender', 'recipient', 'date'].map((value) => ({
            value,
            label: $t('email.signatureFields.' + value),
          })),
        ]"
        :disabled="!can(account, 'edit')"
        class="sm:col-span-2"
      />
      <UiCard
        :title="$t('email.pipeline.title')"
        :description="$t('email.pipeline.help')"
        class="sm:col-span-2"
      >
        <p v-if="profile.pipeline" class="mb-3 break-all text-sm">
          {{ profile.pipeline.pipeline_id }} ·
          {{ profile.pipeline.revision_id.slice(0, 12) }}
        </p>
        <div class="flex flex-wrap gap-2">
          <UiButton
            variant="secondary"
            :disabled="!can(account, 'edit') || saving"
            @click="bindingOpen = true"
            >{{ $t("email.pipeline.select") }}</UiButton
          >
          <UiButton
            v-if="profile.pipeline"
            variant="ghost"
            :disabled="!can(account, 'edit') || saving"
            @click="profile.pipeline = null"
            >{{ $t("email.pipeline.remove") }}</UiButton
          >
        </div>
      </UiCard>
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
  <PipelineLinkPicker v-model="bindingOpen" @select="bindPipeline" />
</template>
