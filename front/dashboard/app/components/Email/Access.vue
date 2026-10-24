<script setup lang="ts">
import { emailRights, rows, object, text, texts } from "~/utils/email";
import type { EmailAccount, EmailRight } from "~/utils/email";
const props = defineProps<{ account: EmailAccount }>();
const api = useEmailApi();
const { t } = useI18n();
const open = defineModel<boolean>("open", { default: false });
const user = ref("");
const selected = ref<EmailRight[]>(["view"]);
const grants = ref<Array<{ user: string; rights: string[] }>>([]);
const error = ref("");
const loading = ref(false);
async function load() {
  try {
    grants.value = rows(
      await api.operation({ operation: "grants", account: props.account.id }),
    ).map((item) => {
      const row = object(item);
      return { user: text(row.user), rights: texts(row.rights) };
    });
  } catch {
    error.value = t("email.loadError");
  }
}
watch(open, async (value) => {
  if (value) {
    user.value = "";
    selected.value = ["view"];
    error.value = "";
    await load();
  }
});
function select(right: EmailRight, enabled: boolean) {
  selected.value = enabled
    ? [...selected.value, right]
    : selected.value.filter((item) => item !== right);
}
async function save() {
  if (loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    await api.operation({
      operation: "grant",
      account: props.account.id,
      user: user.value,
      rights: selected.value,
    });
    user.value = "";
    selected.value = ["view"];
    await load();
  } catch {
    error.value = t("email.saveError");
  } finally {
    loading.value = false;
  }
}
onDeactivated(() => {
  open.value = false;
  grants.value = [];
});
</script>
<template>
  <UiDialog
    v-model:open="open"
    :title="$t('email.manageAccess')"
    :description="$t('email.accessHelp')"
    :close-label="$t('actions.close')"
  >
    <div class="space-y-4">
      <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
      <UiAlert tone="info">{{ $t("email.secretRightHelp") }}</UiAlert>
      <div
        v-for="grant in grants"
        :key="grant.user"
        class="flex flex-wrap items-center gap-2"
      >
        <UiButton
          variant="ghost"
          @click="
            user = grant.user;
            selected = emailRights.filter((right) =>
              grant.rights.includes(right),
            );
          "
          >{{ grant.user }}</UiButton
        ><span class="text-xs text-muted">{{
          grant.rights.map((right) => $t(`email.rights.${right}`)).join(", ")
        }}</span>
      </div>
      <form
        v-if="account.can_manage_access"
        id="email-access"
        class="space-y-4"
        @submit.prevent="save"
      >
        <UiInput
          v-model="user"
          :label="$t('email.memberId')"
          required
          autocomplete="off"
        />
        <div class="grid gap-3 sm:grid-cols-2">
          <UiCheckbox
            v-for="right in emailRights"
            :key="right"
            :model-value="selected.includes(right)"
            :label="$t(`email.rights.${right}`)"
            @update:model-value="(value) => select(right, value === true)"
          />
        </div>
        <p class="text-sm text-muted">{{ $t("email.revokeHelp") }}</p>
      </form>
    </div>
    <template #footer
      ><UiButton variant="secondary" @click="open = false">{{
        $t("actions.close")
      }}</UiButton
      ><UiButton
        v-if="account.can_manage_access"
        type="submit"
        form="email-access"
        :loading="loading"
        :disabled="!account.can_manage_access"
        :pending-label="$t('email.saving')"
        >{{ $t("email.saveAccess") }}</UiButton
      ></template
    >
  </UiDialog>
</template>
