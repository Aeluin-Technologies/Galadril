<script setup lang="ts">
import { toRefs, type UnwrapRef } from "vue";
import type { usePermissions } from "~/composables/usePermissions";

type Controller = Pick<
  UnwrapRef<ReturnType<typeof usePermissions>>,
  | "busy"
  | "subject"
  | "people"
  | "displayName"
  | "roleOptions"
  | "createRole"
  | "newRole"
  | "selectedRole"
  | "assignRole"
  | "parentRole"
  | "includeRole"
  | "targetResource"
  | "resources"
  | "access"
  | "selectedResource"
  | "resourcePermissions"
  | "saveResourceAccess"
  | "resourceAccessReady"
  | "resourceLabel"
  | "newRoleDetails"
  | "newRoleInput"
  | "task"
>;
const props = defineProps<{ controller: Controller }>();
const {
  busy,
  subject,
  people,
  displayName,
  roleOptions,
  createRole,
  newRole,
  selectedRole,
  assignRole,
  parentRole,
  includeRole,
  targetResource,
  resources,
  access,
  selectedResource,
  resourcePermissions,
  saveResourceAccess,
  resourceAccessReady,
  resourceLabel,
  newRoleDetails,
  newRoleInput,
  task,
} = toRefs(props.controller);
</script>
<template>
  <div class="space-y-6">
    <section class="space-y-3">
      <h2>{{ $t("administration.copy.1_choose_a_person_or_role") }}</h2>
      <p>
        {{
          $t(
            "administration.copy.a_role_brings_permissions_together_so_you_can",
          )
        }}
      </p>
      <label for="access-subject">{{
        $t("administration.copy.who_needs_access")
      }}</label
      ><UiSelect
        id="access-subject"
        v-model="subject"
        :options="[
          {
            label: $t('administration.copy.choose_a_person_or_role').trim(),
            value: '',
            disabled: true,
          },
          ...people
            .filter((item) => item.isActive)
            .flatMap((person) => [
              {
                label: String(displayName(person)).trim(),
                value: `user:${person.userId}`,
                group: 'People',
              },
            ]),
          ...roleOptions.flatMap((role) => [
            {
              label: role.displayName || String(role.roleName).trim(),
              value: `role:${role.roleName}`,
              group: 'Roles',
            },
          ]),
        ]"
      />
      <details
        ref="newRoleDetails"
        :open="task === 'create-role'"
        class="mt-5 rounded-xl border border-border p-4"
      >
        <summary class="cursor-pointer text-sm font-medium text-foreground">
          {{ $t("administration.copy.create_a_role") }}
        </summary>
        <form class="mt-3" @submit.prevent="createRole">
          <label for="new-role">{{
            $t("administration.copy.new_role_name")
          }}</label
          ><UiInput
            id="new-role"
            ref="newRoleInput"
            autofocus
            v-model.trim="newRole"
            pattern="[A-Za-z0-9_-]{1,64}"
            maxlength="64"
            required
            :placeholder="$t('administration.copy.for_example_sales_analyst')"
          />
          <p class="hint">
            {{
              $t(
                "administration.copy.use_letters_numbers_hyphens_or_underscores_a_new",
              )
            }}
          </p>
          <UiButton
            type="submit"
            variant="primary"
            :loading="busy"
            :disabled="!newRole"
            class="mt-4"
            >{{ $t("administration.copy.create_role") }}</UiButton
          >
        </form>
      </details>
    </section>
    <section v-if="subject" class="space-y-3 border-t border-border pt-5">
      <h2>{{ $t("administration.copy.2_choose_what_they_can_access") }}</h2>
      <template v-if="subject.startsWith('user:')"
        ><h3 class="mt-4">{{ $t("administration.copy.give_them_a_role") }}</h3>
        <label for="grant-role">{{
          $t("administration.copy.role_to_assign")
        }}</label
        ><UiSelect
          id="grant-role"
          v-model="selectedRole"
          :options="[
            {
              label: $t('administration.copy.choose_a_role').trim(),
              value: '',
              disabled: true,
            },
            ...roleOptions.flatMap((role) => [
              {
                label: role.displayName || String(role.roleName).trim(),
                value: String(role.roleName).trim(),
              },
            ]),
          ]"
        />
        <div class="mt-4 flex flex-wrap gap-3">
          <UiButton
            type="button"
            @click="assignRole()"
            variant="primary"
            :loading="busy"
            :disabled="!selectedRole"
            >{{ $t("administration.copy.assign_role") }}</UiButton
          ><UiButton
            type="button"
            variant="secondary"
            @click="assignRole(false)"
            :loading="busy"
            :disabled="!selectedRole"
            >{{ $t("administration.copy.remove_role") }}</UiButton
          >
        </div></template
      >
      <template v-else
        ><h3 class="mt-4">
          {{ $t("administration.copy.include_another_role_s_access") }}
        </h3>
        <p class="hint">
          {{
            $t(
              "administration.copy.people_with_this_role_will_also_receive_the",
            )
          }}
        </p>
        <label for="included-role">{{
          $t("administration.copy.role_to_include")
        }}</label
        ><UiSelect
          id="included-role"
          v-model="parentRole"
          :options="[
            {
              label: $t('administration.copy.choose_a_role').trim(),
              value: '',
              disabled: true,
            },
            ...roleOptions
              .filter((item) => `role:${item.roleName}` !== subject)
              .flatMap((role) => [
                {
                  label: role.displayName || String(role.roleName).trim(),
                  value: String(role.roleName).trim(),
                },
              ]),
          ]"
        />
        <div class="mt-4 flex gap-3">
          <UiButton
            type="button"
            @click="
              selectedRole = subject.slice(5);
              includeRole();
            "
            variant="primary"
            :loading="busy"
            :disabled="!parentRole"
            >{{ $t("administration.copy.include_role") }}</UiButton
          ><UiButton
            type="button"
            variant="secondary"
            @click="
              selectedRole = subject.slice(5);
              includeRole(false);
            "
            :loading="busy"
            :disabled="!parentRole"
            >{{ $t("administration.copy.remove_inclusion") }}</UiButton
          >
        </div></template
      >
      <div class="mt-6 border-t border-border pt-5">
        <h3>{{ $t("administration.copy.grant_access_to_specific_data") }}</h3>
        <label for="access-resource">{{
          $t("administration.copy.data_resource")
        }}</label
        ><UiSelect
          id="access-resource"
          v-model="targetResource"
          :options="[
            {
              label: $t('administration.copy.choose_a_visible_resource').trim(),
              value: '',
              disabled: true,
            },
            ...resources.flatMap((resource) => [
              {
                label: (
                  String(resource.label) +
                  ' · ' +
                  String(resourceLabel(resource.data.kind))
                ).trim(),
                value: resource.id,
              },
            ]),
          ]"
        />
        <p v-if="!resources.length" class="hint">
          {{
            $t(
              "administration.copy.no_resources_are_visible_in_this_search_clear",
            )
          }}
        </p>
        <label for="resource-access">{{
          $t("administration.copy.permission")
        }}</label
        ><UiSelect
          id="resource-access"
          v-model="access"
          :disabled="busy || !selectedResource"
          :options="[
            {
              label: String(
                selectedResource
                  ? $t('administration.copy.choose_a_permission')
                  : $t('administration.copy.choose_a_data_resource_first'),
              ).trim(),
              value: '',
              disabled: true,
            },
            ...resourcePermissions.flatMap((permission) => [
              {
                label: String(permission.label).trim(),
                value: permission.value,
              },
            ]),
          ]"
        />
        <div class="mt-4 flex flex-wrap gap-3">
          <UiButton
            type="button"
            @click="saveResourceAccess()"
            variant="primary"
            :loading="busy"
            :disabled="!resourceAccessReady"
            >{{ $t("administration.copy.grant_data_access") }}</UiButton
          ><UiButton
            type="button"
            variant="secondary"
            @click="saveResourceAccess(false)"
            :loading="busy"
            :disabled="!resourceAccessReady"
            >{{ $t("administration.copy.remove_data_access") }}</UiButton
          >
        </div>
      </div>
    </section>
  </div>
</template>
