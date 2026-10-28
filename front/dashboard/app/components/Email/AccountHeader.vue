<script setup lang="ts">
import {
  ArrowLeftIcon,
  ArrowPathIcon,
  EllipsisHorizontalIcon,
  PencilSquareIcon,
  ShieldCheckIcon,
  EyeIcon,
  TrashIcon,
} from "@heroicons/vue/24/outline";
import type { MenuItem } from "@galadril/ui/types";
import {
  can,
  emailAccountActions,
  type EmailAccount,
  type EmailAccountAction,
} from "~/utils/email";
const props = defineProps<{
  account: EmailAccount;
  view: string;
  busy: boolean;
}>();
const emit = defineEmits<{ action: [action: EmailAccountAction | "sync"] }>();
const route = useRoute();
const { t } = useI18n();
const actions = computed<MenuItem[]>(() => {
  const labels = {
    test: t("email.test"),
    edit: t("email.senderAndCredentials"),
    servers: t("email.editServers"),
    collection: t("email.editCollection"),
    access: t("email.manageAccess"),
    reveal: t("email.reveal"),
    delete: t("email.delete"),
  };
  const icons = {
    test: ShieldCheckIcon,
    edit: PencilSquareIcon,
    servers: PencilSquareIcon,
    collection: PencilSquareIcon,
    access: ShieldCheckIcon,
    reveal: EyeIcon,
    delete: TrashIcon,
  };
  return emailAccountActions(props.account.rights).map((id) => ({
    id,
    label: labels[id],
    icon: icons[id],
    tone: id === "delete" ? "danger" : "default",
  }));
});
const sections = computed(() =>
  [
    {
      id: "messages",
      label: t(
        props.account.config.imap_host
          ? "email.messagesSection"
          : "email.drafts",
      ),
      visible: can(props.account, "read"),
    },
    {
      id: "replies",
      label: t("email.repliesSection"),
      visible: can(props.account, "use"),
    },
    { id: "connection", label: t("email.connectionSection"), visible: true },
  ].filter((section) => section.visible),
);
function selectAction(id: string) {
  const action = emailAccountActions(props.account.rights).find(
    (action) => action === id,
  );
  if (action) emit("action", action);
}
</script>

<template>
  <UiNavigationLink class="w-fit">
    <NuxtLink
      :to="{
        path: route.path,
        hash: '#email',
        query: { ...route.query, account: undefined, view: undefined },
      }"
    >
      <ArrowLeftIcon aria-hidden="true" />{{ $t("email.allAccounts") }}
    </NuxtLink>
  </UiNavigationLink>
  <UiCard
    :title="account.config.name"
    :description="
      account.config.name !== account.config.address
        ? account.config.address
        : undefined
    "
  >
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex flex-wrap items-center gap-2">
        <UiBadge
          :tone="
            account.sync_status === 'ok'
              ? 'success'
              : account.sync_status === 'needs_attention'
                ? 'warning'
                : 'neutral'
          "
          >{{
            $t(
              account.config.imap_host
                ? `email.sync.${account.sync_status}`
                : "email.sendingOnly",
            )
          }}</UiBadge
        >
        <UiBadge v-if="!account.config.enabled" tone="warning">{{
          $t("email.disabled")
        }}</UiBadge>
        <UiBadge
          :tone="
            account.sending_status === 'verified'
              ? 'success'
              : account.sending_status === 'authentication_failed'
                ? 'warning'
                : 'neutral'
          "
        >
          {{ $t(`email.sending.${account.sending_status ?? "not_checked"}`) }}
        </UiBadge>
      </div>
      <div class="flex items-center gap-2">
        <UiButton
          v-if="can(account, 'use') && account.config.imap_host"
          variant="secondary"
          :loading="busy"
          @click="$emit('action', 'sync')"
          ><ArrowPathIcon aria-hidden="true" />{{
            $t("email.syncNow")
          }}</UiButton
        >
        <UiMenu
          v-if="actions.length"
          :label="$t('email.accountActions')"
          :items="actions"
          :trigger-icon="EllipsisHorizontalIcon"
          icon-only
          variant="ghost"
          :disabled="busy"
          @select="selectAction"
        />
      </div>
    </div>
  </UiCard>
  <nav
    :aria-label="$t('email.accountSections')"
    class="flex gap-2 overflow-x-auto pb-1"
  >
    <UiNavigationLink
      v-for="section in sections"
      :key="section.id"
      :active="view === section.id"
    >
      <NuxtLink
        :to="{
          path: route.path,
          hash: '#email',
          query: { ...route.query, account: account.id, view: section.id },
        }"
        class="shrink-0"
        >{{ section.label }}</NuxtLink
      >
    </UiNavigationLink>
  </nav>
</template>
