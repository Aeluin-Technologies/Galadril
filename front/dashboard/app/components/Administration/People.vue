<script setup lang="ts">
import { toRefs, type UnwrapRef } from "vue";
import type { useAdministration } from "~/composables/useAdministration";
import { ArrowPathIcon } from "@heroicons/vue/24/outline";
import { accountInitials } from "~/utils/account.ts";
type Controller = Pick<
  UnwrapRef<ReturnType<typeof useAdministration>>,
  | "peopleLoaded"
  | "users"
  | "peopleRefreshing"
  | "run"
  | "refreshPeople"
  | "busy"
  | "userSearch"
  | "visibleUsers"
  | "label"
  | "account"
  | "openUser"
  | "peopleAfter"
  | "linkIdentity"
  | "linkDomain"
  | "settings"
  | "externalSubject"
  | "userId"
>;
const props = defineProps<{ controller: Controller }>();
const {
  peopleLoaded,
  users,
  peopleRefreshing,
  run,
  refreshPeople,
  busy,
  userSearch,
  visibleUsers,
  label,
  account,
  openUser,
  peopleAfter,
  linkIdentity,
  linkDomain,
  settings,
  externalSubject,
  userId,
} = toRefs(props.controller);
</script>
<template>
  <template v-if="settings">
    <UiCard class="">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2>{{ $t("administration.copy.your_team") }}</h2>
          <p>
            {{
              peopleLoaded
                ? $t("administration.copy.value0_account_value1", {
                    value0: users.length,
                    value1: users.length === 1 ? "" : "s",
                  })
                : $t("administration.copy.loading_accounts")
            }}
            {{ $t("administration.copy.suspended_accounts_keep_their_data") }}
          </p>
        </div>
        <UiIconButton
          type="button"
          variant="secondary"
          :aria-busy="peopleRefreshing"
          @click="
            run(
              refreshPeople,
              $t('administration.copy.people_updated'),
              $t(
                'administration.copy.your_team_could_not_be_loaded_use_reload',
              ),
            )
          "
          :icon="ArrowPathIcon"
          :label="$t('administration.copy.reload_people')"
          :loading="busy"
          :disabled="peopleRefreshing"
        />
      </div>
      <label for="member-search" class="sr-only">{{
        $t("administration.copy.search_people")
      }}</label
      ><UiInput
        id="member-search"
        v-model="userSearch"
        type="search"
        :placeholder="$t('administration.copy.find_a_person_by_name_or_email')"
        class="mt-5"
      />
      <div class="mt-5 overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead class="border-b border-border text-xs text-muted">
            <tr>
              <th class="py-3 font-medium">
                {{ $t("administration.copy.person") }}
              </th>
              <th class="py-3 font-medium">
                {{ $t("administration.copy.status") }}
              </th>
              <th class="py-3 font-medium text-right">
                {{ $t("administration.copy.access") }}
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-zinc-100">
            <tr v-for="user in visibleUsers" :key="user.userId">
              <td class="py-4 pr-3">
                <div class="flex items-center gap-3">
                  <span
                    class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surface-muted text-xs font-semibold text-muted"
                    >{{ accountInitials(label(user)) }}</span
                  >
                  <div class="min-w-0">
                    <span
                      class="block max-w-64 truncate font-medium text-foreground"
                      >{{ label(user)
                      }}<span
                        v-if="user.userId === account?.subject"
                        class="ml-2 text-xs font-normal text-muted"
                        >{{ $t("administration.copy.you") }}</span
                      ></span
                    ><span
                      v-if="user.displayName && user.email"
                      class="block text-xs text-muted"
                      >{{ user.email }}</span
                    >
                  </div>
                </div>
              </td>
              <td class="py-4">
                <span
                  :class="[
                    'rounded-full px-2 py-1 text-xs',
                    user.isActive
                      ? 'bg-success-subtle text-success'
                      : 'bg-surface-muted text-muted',
                  ]"
                  >{{
                    user.isActive
                      ? $t("administration.copy.active")
                      : $t("administration.copy.suspended")
                  }}</span
                >
              </td>
              <td class="py-4 text-right">
                <UiButton
                  type="button"
                  variant="ghost"
                  :aria-label="
                    $t('administration.copy.manage_value0', {
                      value0: label(user),
                    })
                  "
                  @click="openUser(user)"
                  >{{ $t("administration.copy.manage") }}</UiButton
                >
              </td>
            </tr>
          </tbody>
        </table>
        <p
          v-if="!visibleUsers.length"
          class="py-8 text-center text-sm text-muted"
        >
          {{
            peopleLoaded
              ? $t("administration.copy.no_people_match_your_search")
              : busy
                ? $t("administration.copy.loading_your_team")
                : $t(
                    "administration.copy.the_team_could_not_be_loaded_use_reload",
                  )
          }}
        </p>
      </div>
      <UiButton
        v-if="peopleAfter"
        type="button"
        variant="secondary"
        :disabled="peopleRefreshing"
        @click="
          run(
            () => refreshPeople(true),
            '',
            $t('administration.copy.more_people_could_not_be_loaded_try_again'),
          )
        "
        >{{ $t("administration.copy.load_more_people") }}</UiButton
      >
      <p class="mt-4 text-xs">
        {{
          $t(
            "administration.copy.accounts_come_from_your_connected_sign_in_provider",
          )
        }}
      </p> </UiCard
    ><UiCard class="">
      <details>
        <summary class="cursor-pointer text-sm font-medium text-foreground">
          {{ $t("administration.copy.link_an_existing_account_advanced") }}
        </summary>
        <p class="mt-3">
          {{
            $t(
              "administration.copy.for_your_it_administrator_connect_a_provider_account",
            )
          }}
        </p>
        <form @submit.prevent="linkIdentity">
          <fieldset :disabled="busy">
            <label for="link-domain">{{
              $t("administration.copy.company_domain")
            }}</label
            ><UiSelect
              id="link-domain"
              v-model="linkDomain"
              required
              :options="[
                {
                  label: $t(
                    'administration.copy.choose_a_connected_domain',
                  ).trim(),
                  value: '',
                  disabled: true,
                },
                ...settings.connections.flatMap((connection) => [
                  {
                    label: String(connection.domain).trim(),
                    value: String(connection.domain).trim(),
                  },
                ]),
              ]"
            /><label for="link-subject">{{
              $t("administration.copy.identity_provider_account_identifier")
            }}</label
            ><UiInput
              id="link-subject"
              v-model="externalSubject"
              required
              maxlength="1024"
              autocomplete="off"
            /><label for="link-user">{{
              $t("administration.copy.existing_member")
            }}</label
            ><UiSelect
              id="link-user"
              v-model="userId"
              required
              :options="[
                {
                  label: $t('administration.copy.choose_a_person').trim(),
                  value: '',
                  disabled: true,
                },
                ...users.flatMap((user) => [
                  {
                    label: String(label(user)).trim(),
                    value: user.userId,
                  },
                ]),
              ]"
            /><UiButton type="submit" variant="primary" class="mt-5">{{
              $t("administration.copy.link_account")
            }}</UiButton>
          </fieldset>
        </form>
      </details>
    </UiCard>
  </template>
</template>
