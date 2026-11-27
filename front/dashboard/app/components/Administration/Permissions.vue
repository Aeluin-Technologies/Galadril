<script setup lang="ts">
import { reactive, toRefs } from "vue";
import { ArrowLeftIcon, ArrowRightIcon } from "@heroicons/vue/24/outline";
import type { PermissionProps, Person } from "~/composables/usePermissions";
const props = defineProps<PermissionProps>();
const emit = defineEmits<{
  success: [message: string];
  failure: [message: string];
  settingsChanged: [];
  peopleChanged: [];
  managePerson: [person: Person];
}>();
const permissions = reactive(
  usePermissions(props, {
    success: (message) => emit("success", message),
    failure: (message) => emit("failure", message),
    settingsChanged: () => emit("settingsChanged"),
    peopleChanged: () => emit("peopleChanged"),
    managePerson: (person) => emit("managePerson", person),
  }),
);
const {
  mode,
  roleSearch,
  peopleAfter,
  perform,
  moreDirectory,
  busy,
  ruleRolesAfter,
  loadRuleRoles,
  choices,
  choose,
  confirmImportSettings,
  settings: permissionSettings,
  toggleImports,
  reviewImport,
  applyPermissions,
} = toRefs(permissions);
</script>
<template>
  <div class="space-y-6">
    <div v-if="mode === 'sync' || mode === 'rules'">
      <label for="configuration-role-search" class="sr-only">{{
        $t("administration.copy.search_configuration_roles")
      }}</label
      ><UiInput
        id="configuration-role-search"
        v-model="roleSearch"
        type="search"
        :placeholder="$t('administration.copy.find_a_role')"
      />
      <UiButton
        v-if="mode === 'sync' && peopleAfter"
        variant="secondary"
        :loading="busy"
        @click="
          perform(
            () => moreDirectory('users'),
            '',
            $t('administration.copy.more_people_could_not_be_loaded'),
          )
        "
        >{{ $t("administration.copy.load_more_people") }}</UiButton
      >
      <UiButton
        v-if="ruleRolesAfter"
        variant="secondary"
        :loading="busy"
        @click="
          perform(
            () => loadRuleRoles(true),
            '',
            $t('administration.copy.more_roles_could_not_be_loaded'),
          )
        "
        >{{ $t("administration.copy.load_more_roles") }}</UiButton
      >
    </div>
    <section
      v-if="!mode"
      class="space-y-4"
      :aria-label="$t('administration.copy.permission_options')"
    >
      <div class="rounded-panel border border-border bg-surface p-5">
        <h2 class="font-semibold text-foreground">
          {{ $t("administration.copy.what_would_you_like_to_configure") }}
        </h2>
        <p class="mt-2 text-sm leading-relaxed text-muted">
          {{
            $t(
              "administration.copy.start_with_one_option_roles_decide_what_people",
            )
          }}
        </p>
      </div>
      <UiCard
        ><div class="divide-y divide-border">
          <div v-for="choice in choices" :key="choice.id" class="py-2">
            <UiButton
              type="button"
              variant="ghost"
              align="start"
              class="w-full"
              @click="choose(choice.id)"
            >
              <span class="flex w-full items-center gap-4 py-3"
                ><span class="choice-icon"
                  ><component
                    :is="choice.icon"
                    class="h-6 w-6"
                    aria-hidden="true"
                /></span>
                <span class="min-w-0 flex-1"
                  ><strong
                    class="block text-sm font-semibold text-foreground"
                    >{{ choice.title }}</strong
                  ><span
                    class="mt-1 block text-sm leading-relaxed text-muted"
                    >{{ choice.description }}</span
                  ></span
                >
                <ArrowRightIcon
                  class="h-5 w-5 shrink-0 text-muted"
                  aria-hidden="true"
              /></span>
            </UiButton>
          </div></div
      ></UiCard>
    </section>
    <template v-else>
      <UiButton type="button" variant="ghost" :loading="busy" @click="mode = ''"
        ><ArrowLeftIcon class="mr-2 h-4 w-4" aria-hidden="true" />{{
          $t("administration.copy.all_permission_options")
        }}</UiButton
      >
      <LazyAdministrationPermissionImport
        v-if="mode === 'sync'"
        :controller="permissions"
      />
      <LazyAdministrationPermissionRoles
        v-else-if="mode === 'roles'"
        :controller="permissions"
      />
      <LazyAdministrationPermissionRules v-else :controller="permissions" />
    </template>
    <UiDialog
      :open="confirmImportSettings"
      :title="
        permissionSettings.permission_import
          ? $t('administration.copy.turn_off_permission_imports')
          : $t('administration.copy.allow_permission_imports_2')
      "
      @close="confirmImportSettings = false"
    >
      <p class="text-sm text-muted">
        {{
          permissionSettings.permission_import
            ? $t(
                "administration.copy.existing_access_stays_in_place_new_imports_and",
              )
            : $t(
                "administration.copy.administrators_and_authorized_tools_can_apply_reviewed_permissions",
              )
        }}
      </p>
      <template #footer
        ><UiButton
          type="button"
          variant="secondary"
          :loading="busy"
          @click="confirmImportSettings = false"
          >{{ $t("builder.config_modal.buttons.cancel") }}</UiButton
        ><UiButton
          type="button"
          variant="primary"
          :loading="busy"
          @click="toggleImports().then(() => (confirmImportSettings = false))"
          >{{
            busy
              ? $t("administration.copy.applying")
              : $t("storage.upload.confirm")
          }}</UiButton
        ></template
      >
    </UiDialog>
    <UiDialog
      :open="reviewImport"
      :title="$t('administration.copy.apply_these_access_changes')"
      @close="reviewImport = false"
      ><p class="text-sm text-muted">
        {{
          $t("administration.copy.changes_apply_in_order_if_one_fails_earlier")
        }}
      </p>
      <template #footer
        ><UiButton
          type="button"
          variant="secondary"
          :loading="busy"
          @click="reviewImport = false"
          >{{ $t("builder.config_modal.buttons.cancel") }}</UiButton
        ><UiButton
          type="button"
          variant="primary"
          :loading="busy"
          @click="applyPermissions"
          >{{
            busy
              ? $t("administration.copy.applying")
              : $t("storage.upload.confirm")
          }}</UiButton
        ></template
      ></UiDialog
    >
  </div>
</template>
