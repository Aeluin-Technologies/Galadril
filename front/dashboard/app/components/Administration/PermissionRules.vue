<script setup lang="ts">
import { toRefs, type UnwrapRef } from "vue";
import type { usePermissions } from "~/composables/usePermissions";

type Controller = Pick<
  UnwrapRef<ReturnType<typeof usePermissions>>,
  | "ruleMode"
  | "ruleStep"
  | "busy"
  | "ruleName"
  | "roleSearch"
  | "ruleScope"
  | "scopedRoles"
  | "start"
  | "end"
  | "reviewRule"
  | "ruleId"
  | "policies"
  | "saveRule"
  | "savePolicy"
  | "policyId"
  | "policyContent"
  | "policyActive"
  | "editPolicy"
>;
const props = defineProps<{ controller: Controller }>();
const {
  ruleMode,
  ruleStep,
  busy,
  ruleName,
  roleSearch,
  ruleScope,
  scopedRoles,
  start,
  end,
  reviewRule,
  ruleId,
  policies,
  saveRule,
  savePolicy,
  policyId,
  policyContent,
  policyActive,
  editPolicy,
} = toRefs(props.controller);
</script>
<template>
  <UiCard class="">
    <h2>{{ $t("administration.copy.general_access_rules") }}</h2>
    <p>
      {{
        $t(
          "administration.copy.these_rules_restrict_existing_access_they_do_not",
        )
      }}
    </p>
    <div
      class="mt-5 flex w-fit gap-1 rounded-lg bg-surface-muted p-1"
      :aria-label="$t('administration.copy.general_rule_editor')"
    >
      <UiButton
        type="button"
        :variant="ruleMode === 'blocks' ? 'secondary' : 'ghost'"
        :aria-pressed="ruleMode === 'blocks'"
        @click="ruleMode = 'blocks'"
        >{{ $t("administration.copy.guided_settings") }}</UiButton
      ><UiButton
        type="button"
        :variant="ruleMode === 'code' ? 'secondary' : 'ghost'"
        :aria-pressed="ruleMode === 'code'"
        @click="ruleMode = 'code'"
        >{{ $t("administration.copy.cedar_code") }}</UiButton
      >
    </div>
    <template v-if="ruleMode === 'blocks'"
      ><ol
        class="steps"
        :aria-label="$t('administration.copy.general_access_rule_progress')"
      >
        <li :aria-current="ruleStep === 1 ? 'step' : undefined">
          {{ $t("administration.copy.1_who") }}
        </li>
        <li :aria-current="ruleStep === 2 ? 'step' : undefined">
          {{ $t("administration.copy.2_when") }}
        </li>
        <li :aria-current="ruleStep === 3 ? 'step' : undefined">
          {{ $t("administration.copy.3_review") }}
        </li>
      </ol>
      <fieldset :disabled="busy">
        <template v-if="ruleStep === 1"
          ><h3>
            {{ $t("administration.copy.who_should_this_rule_apply_to") }}
          </h3>
          <label for="rule-name">{{
            $t("administration.copy.rule_name")
          }}</label>
          <UiInput
            id="rule-name"
            v-model="ruleName"
            maxlength="128"
            required
            :placeholder="$t('administration.copy.for_example_office_hours')"
          />
          <p class="hint">
            {{
              $t(
                "administration.copy.give_each_access_rule_its_own_name_reusing",
              )
            }}
          </p>
          <label for="rule-role-search" class="sr-only">{{
            $t("administration.copy.find_a_role")
          }}</label
          ><UiInput
            id="rule-role-search"
            v-model="roleSearch"
            type="search"
            :placeholder="$t('administration.copy.find_a_role')"
            maxlength="128"
            class="mt-4"
          />
          <label for="rule-scope">{{
            $t("administration.copy.rule_scope")
          }}</label
          ><UiSelect
            id="rule-scope"
            v-model="ruleScope"
            :options="[
              {
                label: $t(
                  'administration.copy.everyone_except_administrators',
                ).trim(),
                value: '',
              },
              ...scopedRoles.flatMap((role) => [
                {
                  label: role.displayName || String(role.roleName).trim(),
                  value: String(role.roleName).trim(),
                },
              ]),
            ]"
          />
          <p class="hint">
            {{ $t("administration.copy.choose_a_team_role_or_apply_the_rule") }}
          </p>
          <UiButton
            type="button"
            @click="ruleStep = 2"
            variant="primary"
            class="mt-5"
            >{{ $t("authentication.continue") }}</UiButton
          ></template
        >
        <template v-else-if="ruleStep === 2"
          ><h3>
            {{ $t("administration.copy.when_should_access_be_available") }}
          </h3>
          <p>
            {{
              $t(
                "administration.copy.hours_use_utc_so_they_stay_consistent_across",
              )
            }}
          </p>
          <div class="grid gap-4 sm:grid-cols-2">
            <label for="access-start"
              >{{ $t("administration.copy.access_starts_at_utc")
              }}<UiInput
                id="access-start"
                v-model="start"
                type="time"
                step="3600"
                required /></label
            ><label for="access-end"
              >{{ $t("administration.copy.access_ends_at_utc")
              }}<UiInput
                id="access-end"
                v-model="end"
                type="time"
                step="3600"
                required
            /></label>
          </div>
          <div class="mt-5 rounded-xl border border-border bg-canvas p-4">
            <h3>
              {{ $t("administration.copy.approximate_connection_location") }}
            </h3>
            <p class="hint">
              {{
                $t(
                  "administration.copy.location_rules_require_a_trusted_location_provider_this",
                )
              }}
            </p>
          </div>
          <div class="mt-5 flex gap-3">
            <UiButton type="button" variant="secondary" @click="ruleStep = 1">{{
              $t("administration.copy.back")
            }}</UiButton
            ><UiButton type="button" @click="reviewRule" variant="primary">{{
              $t("administration.copy.review_rule")
            }}</UiButton>
          </div></template
        >
        <template v-else
          ><h3>{{ $t("administration.copy.review_your_access_window") }}</h3>
          <dl
            class="mt-4 space-y-2 rounded-xl bg-primary-subtle/60 p-4 text-sm"
          >
            <dt class="text-muted">
              {{ $t("administration.copy.rule_name") }}
            </dt>
            <dd class="font-medium text-foreground">{{ ruleName }}</dd>
            <dt class="text-muted">{{ $t("administration.copy.saved_as") }}</dt>
            <dd class="font-medium text-foreground">{{ ruleId }}</dd>
            <dt class="text-muted">
              {{ $t("administration.copy.applies_to") }}
            </dt>
            <dd class="font-medium text-foreground">
              {{
                ruleScope ||
                $t("administration.copy.everyone_except_administrators")
              }}
            </dd>
            <dt class="text-muted">
              {{ $t("administration.copy.allowed_hours") }}
            </dt>
            <dd class="font-medium text-foreground">
              {{ start }}–{{ end }} {{ $t("administration.copy.utc") }}
            </dd>
          </dl>
          <p class="hint">
            {{
              policies.some((policy) => policy.id === ruleId)
                ? $t(
                    "administration.copy.saving_updates_the_existing_rule_value0",
                    { value0: ruleId },
                  )
                : $t("administration.copy.saving_creates_a_new_rule_value0", {
                    value0: ruleId,
                  })
            }}
            {{
              $t(
                "administration.copy.other_saved_rules_continue_to_apply_administrators_keep",
              )
            }}
          </p>
          <div class="mt-5 flex gap-3">
            <UiButton type="button" variant="secondary" @click="ruleStep = 2">{{
              $t("administration.copy.back")
            }}</UiButton
            ><UiButton type="button" @click="saveRule" variant="primary">{{
              $t("administration.copy.save_rule")
            }}</UiButton>
          </div></template
        >
      </fieldset></template
    >
    <form v-else class="mt-5" @submit.prevent="savePolicy()">
      <p>
        {{ $t("administration.copy.for_advanced_conditions_use_cedar") }}
        <a
          href="https://docs.cedarpolicy.com/"
          target="_blank"
          rel="noopener noreferrer"
          class="font-medium text-primary-foreground underline"
          >{{ $t("administration.copy.read_the_cedar_documentation") }}</a
        >.
      </p>
      <p class="hint">
        {{
          $t(
            "administration.copy.available_request_attributes_include_utc_hour_and_verified",
          )
        }}
      </p>
      <label for="cedar-id">{{ $t("administration.copy.policy_name") }}</label
      ><UiInput
        id="cedar-id"
        v-model.trim="policyId"
        maxlength="64"
        pattern="[A-Za-z0-9_-]{1,64}"
        required
      /><label for="cedar-code">{{
        $t("administration.copy.cedar_policy")
      }}</label
      ><UiTextarea
        id="cedar-code"
        v-model="policyContent"
        rows="10"
        maxlength="8192"
        required
        spellcheck="false"
        :placeholder="
          $t(
            'administration.copy.forbid_principal_action_resource_when_context_hour_utc',
          )
        "
      /><label class="check"
        ><UiCheckbox v-model="policyActive" type="checkbox" />{{
          $t("administration.copy.enable_this_policy")
        }}</label
      ><UiButton
        type="submit"
        variant="primary"
        :loading="busy"
        :disabled="!policyContent.trim()"
        class="mt-5"
        >{{ $t("administration.copy.save_cedar_policy") }}</UiButton
      >
    </form> </UiCard
  ><UiCard v-if="policies.length" class="">
    <h2>{{ $t("administration.copy.saved_rules") }}</h2>
    <p>
      {{ $t("administration.copy.review_the_code_or_pause_an_existing_rule") }}
    </p>
    <div
      v-for="policy in policies"
      :key="policy.id"
      class="flex flex-wrap items-center justify-between gap-3 border-b border-border py-4 last:border-0"
    >
      <span class="text-sm font-medium text-foreground"
        >{{ policy.id
        }}<span class="ml-2 text-xs font-normal text-muted">{{
          policy.active
            ? $t("administration.copy.active")
            : $t("administration.copy.paused")
        }}</span></span
      >
      <div class="flex gap-2">
        <UiButton
          type="button"
          variant="ghost"
          @click="editPolicy(policy)"
          :loading="busy"
          >{{ $t("ui.actions.edit") }}</UiButton
        ><UiButton
          type="button"
          variant="secondary"
          @click="savePolicy({ ...policy, active: !policy.active })"
          :loading="busy"
          >{{
            policy.active
              ? $t("administration.copy.pause_rule")
              : $t("administration.copy.enable_rule")
          }}</UiButton
        >
      </div>
    </div>
  </UiCard>
</template>
