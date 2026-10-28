<script setup lang="ts">
import { EnvelopeIcon, ArrowRightIcon } from "@heroicons/vue/24/outline";
import type { EmailAccount } from "~/utils/email";
defineProps<{ accounts: readonly EmailAccount[] }>();
const route = useRoute();
</script>

<template>
  <UiCard
    :title="$t('email.chooseAccount')"
    :description="$t('email.chooseAccountHelp')"
  >
    <div class="divide-y divide-border">
      <div v-for="account in accounts" :key="account.id" class="py-2">
        <UiNavigationLink>
          <NuxtLink
            :to="{
              path: route.path,
              hash: '#email',
              query: { ...route.query, account: account.id, view: undefined },
            }"
          >
            <span class="flex min-w-0 flex-1 items-center gap-4 py-3">
              <EnvelopeIcon class="h-6 w-6 shrink-0" aria-hidden="true" />
              <span class="min-w-0 flex-1">
                <span class="block break-all font-semibold">{{
                  account.config.name
                }}</span>
                <span class="mt-1 block text-xs">{{
                  $t(`email.sending.${account.sending_status ?? "not_checked"}`)
                }}</span>
                <span
                  v-if="account.config.address !== account.config.name"
                  class="mt-1 block break-all text-sm"
                  >{{ account.config.address }}</span
                >
                <span class="mt-1 block text-xs">{{
                  $t(
                    account.config.imap_host
                      ? `email.sync.${account.sync_status}`
                      : "email.sendingOnly",
                  )
                }}</span>
              </span>
              <ArrowRightIcon class="h-5 w-5 shrink-0" aria-hidden="true" />
            </span>
          </NuxtLink>
        </UiNavigationLink>
      </div>
    </div>
  </UiCard>
</template>
