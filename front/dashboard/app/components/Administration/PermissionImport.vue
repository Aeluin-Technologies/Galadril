<script setup lang="ts">
import { toRefs, type UnwrapRef } from "vue";
import type { usePermissions } from "~/composables/usePermissions";

type Controller = Pick<
  UnwrapRef<ReturnType<typeof usePermissions>>,
  | "settings"
  | "busy"
  | "confirmImportSettings"
  | "syncStep"
  | "synchronization"
  | "selectConnection"
  | "ruleRoles"
  | "saveSynchronization"
  | "reviewPermissions"
  | "manifest"
  | "people"
>;
const props = defineProps<{ controller: Controller }>();
const {
  settings,
  busy,
  confirmImportSettings,
  syncStep,
  synchronization,
  selectConnection,
  ruleRoles,
  saveSynchronization,
  reviewPermissions,
  manifest,
  people,
} = toRefs(props.controller);
</script>
<template>
  <UiCard class="">
    <h2>{{ $t("administration.copy.reuse_your_company_s_groups") }}</h2>
    <p>
      {{ $t("administration.copy.your_sign_in_provider_sends_a_person_s") }}
    </p>
    <label class="check"
      ><UiCheckbox
        type="checkbox"
        :checked="settings.permission_import"
        :disabled="busy"
        @click.prevent="confirmImportSettings = true"
      />{{ $t("administration.copy.allow_permission_imports") }}</label
    >
    <p class="hint">
      {{
        $t(
          "administration.copy.off_by_default_this_enables_approved_imports_and",
        )
      }}
    </p>
    <ol
      class="steps"
      :aria-label="$t('administration.copy.role_synchronization_progress')"
    >
      <li :aria-current="syncStep === 1 ? 'step' : undefined">
        {{ $t("administration.copy.1_company_sign_in") }}
      </li>
      <li :aria-current="syncStep === 2 ? 'step' : undefined">
        {{ $t("administration.copy.2_match_groups") }}
      </li>
      <li :aria-current="syncStep === 3 ? 'step' : undefined">
        {{ $t("administration.copy.3_review") }}
      </li>
    </ol>
    <fieldset :disabled="busy">
      <template v-if="syncStep === 1">
        <label for="sync-connection">{{
          $t("administration.copy.company_sign_in_connection")
        }}</label
        ><UiSelect
          id="sync-connection"
          v-model="synchronization.connectionId"
          :options="[
            {
              label: $t(
                'administration.copy.choose_a_connected_company_domain',
              ).trim(),
              value: '',
              disabled: true,
            },
            ...settings.connections
              .filter((item) => item.enabled && item.connection_id)
              .flatMap((connection) => [
                {
                  label: String(connection.domain).trim(),
                  value: connection.connection_id ?? '',
                },
              ]),
          ]"
          @update:model-value="selectConnection"
        />
        <p
          v-if="
            !settings.connections.some(
              (item) => item.enabled && item.connection_id,
            )
          "
          class="hint"
        >
          {{
            $t("administration.copy.connect_your_company_sign_in_under_sign_in")
          }}
        </p>
        <details class="mt-4 rounded-xl border border-border p-4">
          <summary class="cursor-pointer text-sm text-muted">
            {{ $t("administration.copy.provider_attribute_advanced") }}
          </summary>
          <label for="sync-claim">{{
            $t("administration.copy.group_attribute")
          }}</label
          ><UiInput
            id="sync-claim"
            v-model.trim="synchronization.claim"
            maxlength="64"
          />
          <p class="hint">
            {{
              $t(
                "administration.copy.your_it_team_can_confirm_the_signed_group",
              )
            }}
          </p>
        </details>
        <UiButton
          type="button"
          :disabled="!synchronization.connectionId"
          @click="syncStep = 2"
          variant="primary"
          class="mt-5"
          >{{ $t("authentication.continue") }}</UiButton
        >
      </template>
      <template v-else-if="syncStep === 2">
        <h3>
          {{
            $t("administration.copy.what_should_each_company_group_be_able_to")
          }}
        </h3>
        <p>
          {{
            $t("administration.copy.use_the_exact_group_name_supplied_by_your")
          }}
        </p>
        <div
          v-for="(mapping, index) in synchronization.mappings"
          :key="index"
          class="mt-4 grid gap-3 rounded-xl bg-canvas p-4 sm:grid-cols-2"
        >
          <label :for="`external-group-${index}`"
            >{{ $t("administration.copy.company_group")
            }}<UiInput
              :id="`external-group-${index}`"
              v-model.trim="mapping.externalGroup"
              maxlength="256"
              required /></label
          ><label :for="`internal-role-${index}`"
            >{{ $t("administration.copy.galadril_role")
            }}<UiSelect
              :id="`internal-role-${index}`"
              v-model="mapping.roleName"
              required
              :options="[
                {
                  label: $t('administration.copy.choose_a_role').trim(),
                  value: '',
                  disabled: true,
                },
                ...ruleRoles.flatMap((role) => [
                  {
                    label: role.displayName || String(role.roleName).trim(),
                    value: String(role.roleName).trim(),
                  },
                ]),
              ]" /></label
          ><UiButton
            type="button"
            variant="ghost"
            :aria-label="
              $t('administration.copy.remove_group_match_value0', {
                value0: index + 1,
              })
            "
            @click="synchronization.mappings.splice(index, 1)"
            >{{ $t("administration.copy.remove") }}</UiButton
          >
        </div>
        <UiButton
          type="button"
          variant="secondary"
          :disabled="synchronization.mappings.length >= 64"
          @click="
            synchronization.mappings.push({
              externalGroup: '',
              roleName: '',
            })
          "
          class="mt-4"
          >{{ $t("administration.copy.add_a_group_match") }}</UiButton
        >
        <div class="mt-5 flex gap-3">
          <UiButton type="button" variant="secondary" @click="syncStep = 1">{{
            $t("administration.copy.back")
          }}</UiButton
          ><UiButton
            type="button"
            :disabled="
              !synchronization.mappings.length ||
              synchronization.mappings.some(
                (item) => !item.externalGroup || !item.roleName,
              )
            "
            @click="syncStep = 3"
            variant="primary"
            >{{ $t("administration.copy.review_matches") }}</UiButton
          >
        </div>
      </template>
      <template v-else>
        <h3>{{ $t("administration.copy.review_company_access") }}</h3>
        <ul class="mt-4 space-y-2 text-sm text-muted">
          <li
            v-for="mapping in synchronization.mappings"
            :key="mapping.externalGroup"
          >
            <strong>{{ mapping.externalGroup }}</strong> →
            {{ mapping.roleName }}
          </li>
        </ul>
        <label class="check"
          ><UiCheckbox v-model="synchronization.enabled" type="checkbox" />{{
            $t("administration.copy.synchronize_these_roles_at_company_sign_in")
          }}</label
        >
        <p class="hint">
          {{
            $t(
              "administration.copy.only_these_mapped_memberships_are_synchronized_locally_granted",
            )
          }}
        </p>
        <div class="mt-5 flex gap-3">
          <UiButton type="button" variant="secondary" @click="syncStep = 2">{{
            $t("administration.copy.back")
          }}</UiButton
          ><UiButton
            type="button"
            :disabled="synchronization.enabled && !settings.permission_import"
            @click="saveSynchronization"
            variant="primary"
            >{{ $t("administration.copy.save_synchronization") }}</UiButton
          >
        </div>
      </template>
    </fieldset> </UiCard
  ><UiCard class="">
    <summary class="cursor-pointer text-sm font-medium text-foreground">
      {{ $t("administration.copy.import_a_configuration_advanced") }}
    </summary>
    <p>
      {{ $t("administration.copy.for_your_it_team_or_automation_prepare_a") }}
    </p>
    <form class="mt-5 space-y-4" @submit.prevent="reviewPermissions">
      <AdministrationPermissionEditor
        v-model="manifest"
        :users="people"
        :roles="ruleRoles"
        :disabled="busy"
      /><UiButton
        type="submit"
        :disabled="busy || !settings.permission_import || !manifest.trim()"
        variant="primary"
        >{{ $t("administration.copy.review_changes") }}</UiButton
      >
    </form>
  </UiCard>
</template>
