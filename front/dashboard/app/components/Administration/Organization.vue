<script setup lang="ts">
import { toRefs, type UnwrapRef } from "vue";
import type { useAdministration } from "~/composables/useAdministration";

type Controller = Pick<
  UnwrapRef<ReturnType<typeof useAdministration>>,
  "saveOrganization" | "organizationName" | "busy"
>;
const props = defineProps<{ controller: Controller }>();
const { saveOrganization, organizationName, busy } = toRefs(props.controller);
</script>
<template>
  <UiCard as="form" class="" @submit.prevent="saveOrganization">
    <h2>{{ $t("administration.copy.make_it_yours") }}</h2>
    <p>
      {{
        $t(
          "administration.copy.this_name_helps_your_team_recognize_their_organization",
        )
      }}
    </p>
    <label for="organization-name">{{
      $t("administration.copy.organization_name")
    }}</label>
    <UiInput
      id="organization-name"
      v-model.trim="organizationName"
      required
      maxlength="128"
      :placeholder="$t('administration.copy.your_company_or_team_name')"
      autocomplete="organization"
    />
    <UiButton
      type="submit"
      variant="primary"
      :loading="busy"
      :disabled="!organizationName.trim()"
      class="mt-5"
      >{{ $t("administration.copy.save_changes") }}</UiButton
    > </UiCard
  ><UiCard class="">
    <h2>{{ $t("administration.copy.set_up_your_team") }}</h2>
    <p>
      {{ $t("administration.copy.start_here_you_can_come_back_and_change") }}
    </p>
    <NuxtLink
      v-for="(item, index) in [
        {
          id: 'users',
          title: $t('administration.copy.meet_your_members'),
          text: $t(
            'administration.copy.review_accounts_and_give_each_person_the_access',
          ),
        },
        {
          id: 'domains',
          title: $t('administration.copy.connect_your_company_sign_in'),
          text: $t(
            'administration.copy.follow_three_steps_to_use_your_existing_work',
          ),
        },
        {
          id: 'permissions',
          title: $t('administration.copy.choose_your_team_s_permissions'),
          text: $t(
            'administration.copy.reuse_company_roles_manage_access_or_set_general',
          ),
        },
      ]"
      :key="item.id"
      :to="`/settings/sso#${item.id}`"
      class="mt-4 flex items-start gap-4 rounded-xl border border-border p-4 hover:border-border hover:bg-primary-subtle/50 transition"
    >
      <span
        class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-surface-muted text-xs font-semibold text-muted"
        >{{ index + 1 }}</span
      >
      <span
        ><strong class="block text-sm font-medium text-foreground">{{
          item.title
        }}</strong
        ><span class="mt-1 block text-xs leading-relaxed text-muted">{{
          item.text
        }}</span></span
      >
    </NuxtLink>
  </UiCard>
</template>
