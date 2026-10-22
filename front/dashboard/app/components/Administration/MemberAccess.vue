<script setup lang="ts">
import { toRefs, type UnwrapRef } from "vue";
import type { useAdministration } from "~/composables/useAdministration";

type Controller = Pick<
  UnwrapRef<ReturnType<typeof useAdministration>>,
  | "selectedUser"
  | "renamePerson"
  | "label"
  | "changeStatus"
  | "busy"
  | "account"
  | "message"
  | "pending"
  | "failed"
  | "roleSearch"
  | "roles"
  | "hasRole"
  | "rolesRefreshing"
  | "changeRole"
  | "rolesAfter"
  | "run"
  | "refreshUserRoles"
  | "resetKey"
>;
const props = defineProps<{ controller: Controller }>();
const {
  selectedUser,
  renamePerson,
  label,
  changeStatus,
  busy,
  account,
  message,
  pending,
  failed,
  roleSearch,
  roles,
  hasRole,
  rolesRefreshing,
  changeRole,
  rolesAfter,
  run,
  refreshUserRoles,
  resetKey,
} = toRefs(props.controller);
</script>
<template>
  <UiDialog
    :open="!!selectedUser"
    :title="
      selectedUser
        ? $t('administration.copy.access_for_value0', {
            value0: label(selectedUser),
          })
        : ''
    "
    @close="selectedUser = undefined"
  >
    <p
      v-if="message && !pending"
      :role="failed ? 'alert' : 'status'"
      :class="[
        'mb-4 rounded-lg border p-3 text-sm',
        failed
          ? 'border-danger bg-danger-subtle text-danger'
          : 'border-border bg-primary-subtle text-primary-foreground',
      ]"
    >
      {{ message }}
    </p>
    <template v-if="selectedUser">
      <AdministrationDisplayNameForm
        class="mb-6"
        :current-name="label(selectedUser)"
        :busy="busy"
        @save="renamePerson"
      />
      <h3 class="text-sm font-semibold text-foreground">
        {{ $t("administration.copy.roles") }}
      </h3>
      <p class="mt-1 text-xs text-muted">
        {{
          $t(
            "administration.copy.roles_collect_the_access_configured_by_your_organization",
          )
        }}
      </p>
      <label for="person-role-search" class="sr-only">{{
        $t("administration.copy.search_roles")
      }}</label
      ><UiInput
        id="person-role-search"
        v-model="roleSearch"
        type="search"
        :placeholder="$t('administration.copy.find_a_role')"
      />
      <label v-for="role in roles" :key="role.roleName" class="check"
        ><UiCheckbox
          type="checkbox"
          :checked="hasRole(selectedUser.userId, role.roleName)"
          :disabled="busy || rolesRefreshing || !selectedUser.isActive"
          @click.prevent="
            changeRole(
              selectedUser,
              role.roleName,
              !hasRole(selectedUser.userId, role.roleName),
            )
          "
        />{{ role.displayName || role.roleName }}</label
      >
      <UiButton
        v-if="rolesAfter"
        type="button"
        variant="secondary"
        :disabled="rolesRefreshing || busy"
        @click="
          run(
            () => refreshUserRoles(true),
            '',
            $t('administration.copy.more_roles_could_not_be_loaded_try_again'),
          )
        "
        >{{ $t("administration.copy.load_more_roles") }}</UiButton
      >
      <p
        v-if="!roles.length"
        class="mt-4 rounded-lg bg-canvas p-3 text-sm text-muted"
      >
        {{ $t("administration.copy.no_roles_have_been_configured_yet") }}
      </p>
      <div class="mt-6 border-t border-border pt-4 space-y-3">
        <h3 class="text-sm font-semibold text-foreground">
          {{ $t("administration.copy.account_recovery") }}
        </h3>
        <p class="text-xs text-muted">
          {{
            $t(
              "administration.copy.reset_a_lost_passkey_only_after_verifying_the",
            )
          }}
        </p>
        <UiButton
          type="button"
          variant="secondary"
          @click="resetKey(selectedUser)"
          :loading="busy"
          :disabled="!selectedUser.isActive"
          >{{ $t("administration.copy.reset_passkey") }}</UiButton
        >
      </div></template
    >
    <template #footer
      ><UiButton
        type="button"
        variant="secondary"
        @click="selectedUser = undefined"
        >{{ $t("administration.copy.done") }}</UiButton
      ><UiButton
        v-if="selectedUser"
        type="button"
        :variant="selectedUser.isActive ? 'danger' : 'primary'"
        @click="changeStatus(selectedUser)"
        :loading="busy"
        :disabled="selectedUser.userId === account?.subject"
        >{{
          selectedUser.isActive
            ? $t("administration.copy.suspend_access")
            : $t("administration.copy.restore_access")
        }}</UiButton
      ></template
    >
  </UiDialog>
</template>
