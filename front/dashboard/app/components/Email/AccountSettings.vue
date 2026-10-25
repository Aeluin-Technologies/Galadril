<script setup lang="ts">
import {
  EllipsisHorizontalIcon,
  PencilSquareIcon,
  ShieldCheckIcon,
} from "@heroicons/vue/24/outline";
import type { MenuItem } from "@galadril/ui/types";
import {
  can,
  emailAccountActions,
  type EmailAccount,
  type EmailAccountAction,
} from "~/utils/email";
const props = defineProps<{ account: EmailAccount; busy: boolean }>();
const emit = defineEmits<{ action: [action: EmailAccountAction] }>();
const { t } = useI18n();
const serverActions = computed<MenuItem[]>(() =>
  emailAccountActions(props.account.rights, "servers").map((id) => ({
    id,
    label: id === "test" ? t("email.test") : t("email.editServers"),
    icon: id === "test" ? ShieldCheckIcon : PencilSquareIcon,
  })),
);
function serverAction(id: string) {
  if (id === "test" || id === "servers") emit("action", id);
}
</script>

<template>
  <UiCard
    :title="$t('email.connectionSection')"
    :description="$t('email.connectionSectionHelp')"
  >
    <template #actions
      ><UiMenu
        v-if="serverActions.length"
        :label="$t('email.serverActions')"
        :items="serverActions"
        :trigger-icon="EllipsisHorizontalIcon"
        icon-only
        variant="ghost"
        :disabled="busy"
        @select="serverAction"
    /></template>
    <dl class="grid gap-5 sm:grid-cols-2">
      <div v-if="account.config.imap_host">
        <dt class="text-sm text-muted">{{ $t("email.imapServer") }}</dt>
        <dd class="mt-1 break-all">
          {{ account.config.imap_host }}:{{ account.config.imap_port }}
        </dd>
      </div>
      <div>
        <dt class="text-sm text-muted">{{ $t("email.smtpServer") }}</dt>
        <dd class="mt-1 break-all">
          {{ account.config.smtp_host }}:{{ account.config.smtp_port }}
        </dd>
      </div>
    </dl>
  </UiCard>
  <UiCard
    v-if="account.config.imap_host"
    :title="$t('email.collectionSection')"
  >
    <template #actions
      ><UiIconButton
        v-if="can(account, 'edit')"
        :label="$t('email.editCollection')"
        :icon="PencilSquareIcon"
        variant="ghost"
        :disabled="busy"
        @click="$emit('action', 'collection')"
    /></template>
    <dl class="grid gap-5 sm:grid-cols-2">
      <div>
        <dt class="text-sm text-muted">{{ $t("email.folders") }}</dt>
        <dd class="mt-1 break-all">{{ account.config.folders.join(", ") }}</dd>
      </div>
      <div>
        <dt class="text-sm text-muted">{{ $t("email.pollSeconds") }}</dt>
        <dd class="mt-1">{{ account.config.poll_seconds }}</dd>
      </div>
    </dl>
  </UiCard>
  <UiCard
    v-if="can(account, 'share') || can(account, 'delete')"
    :title="$t('email.connectionManagement')"
  >
    <div class="flex flex-wrap gap-3">
      <UiButton
        v-if="can(account, 'share')"
        variant="secondary"
        :disabled="busy"
        @click="$emit('action', 'access')"
        >{{ $t("email.manageAccess") }}</UiButton
      >
      <UiButton
        v-if="can(account, 'delete')"
        variant="danger"
        :disabled="busy"
        @click="$emit('action', 'delete')"
        >{{ $t("email.delete") }}</UiButton
      >
    </div>
  </UiCard>
</template>
