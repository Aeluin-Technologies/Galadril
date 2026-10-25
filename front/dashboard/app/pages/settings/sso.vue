<script setup lang="ts">
import SettingsNavigation from "~/components/Settings/Navigation.vue";
const { t } = useI18n();

import { reactive, toRefs } from "vue";
definePageMeta({ middleware: ["administrator"] });
useHead({ title: () => t("administration.copy.administration_galadril") });
const activeSection = ref("organization");
const panel = ref<HTMLElement>();
const sections = computed(() => [
  {
    id: "organization",
    label: t("administration.copy.organization"),
    description: t("administration.copy.your_team_s_home"),
  },
  {
    id: "users",
    label: t("administration.copy.people"),
    description: t("administration.copy.members_and_their_access"),
  },
  {
    id: "domains",
    label: t("administration.copy.sign_in_security"),
    description: t("administration.copy.how_your_team_connects"),
  },
  {
    id: "permissions",
    label: t("administration.copy.permissions"),
    description: t("administration.copy.who_can_access_what"),
  },
]);
const route = useRoute();
watch(
  () => route.hash,
  (hash) => {
    const section = hash.slice(1);
    activeSection.value = sections.value.some((item) => item.id === section)
      ? section
      : "organization";
    nextTick(() => panel.value?.scrollTo({ top: 0 }));
  },
  { immediate: true },
);
const administration = reactive(useAdministration());
const {
  message,
  failed,
  settings,
  busy,
  run,
  refresh,
  refreshPeople,
  users,
  roles,
  graphql,
  pending,
  applyConfirmed,
} = toRefs(administration);
</script>
<template>
  <section
    data-administration
    ref="panel"
    :id="activeSection"
    :aria-label="$t('administration.copy.administration')"
    class="h-full min-h-0 overflow-y-auto bg-canvas"
  >
    <div class="mx-auto max-w-6xl px-5 py-8 sm:px-8 lg:py-10">
      <div class="flex flex-col gap-8 lg:flex-row lg:gap-12">
        <SettingsNavigation
          :label="$t('administration.copy.organization_settings')"
          :sections="sections"
          :active="activeSection"
          base="/settings/sso"
        />
        <div class="min-w-0 flex-1 space-y-6">
          <header>
            <h1 class="text-2xl font-semibold tracking-tight text-foreground">
              {{
                activeSection === "organization"
                  ? $t("administration.copy.your_organization")
                  : activeSection === "users"
                    ? $t("administration.copy.people")
                    : activeSection === "domains"
                      ? $t("administration.copy.sign_in_security")
                      : $t("administration.copy.permissions")
              }}
            </h1>
            <p class="mt-2 text-sm leading-relaxed text-muted">
              {{
                activeSection === "organization"
                  ? $t(
                      "administration.copy.a_few_simple_settings_to_make_galadril_work",
                    )
                  : activeSection === "users"
                    ? $t(
                        "administration.copy.see_who_can_use_galadril_and_manage_what",
                      )
                    : activeSection === "domains"
                      ? $t(
                          "administration.copy.let_your_team_sign_in_with_the_account",
                        )
                      : $t(
                          "administration.copy.choose_who_can_access_your_data_and_when",
                        )
              }}
            </p>
          </header>
          <p
            v-if="message"
            :role="failed ? 'alert' : 'status'"
            :aria-label="$t('administration.copy.feedback')"
            :class="[
              'sticky top-3 z-10 rounded-xl border p-4 text-sm shadow-sm',
              failed
                ? 'border-danger bg-danger-subtle text-danger'
                : 'border-border bg-primary-subtle text-primary-foreground',
            ]"
          >
            {{ message }}
          </p>
          <p v-if="!settings && busy" role="status" class="text-sm text-muted">
            {{ $t("administration.copy.loading_your_organization") }}
          </p>
          <UiButton
            v-if="failed && !settings"
            type="button"
            variant="secondary"
            @click="
              run(async () => {
                await refresh();
                await refreshPeople();
              }, '')
            "
            :loading="busy"
            >{{ $t("administration.copy.try_again") }}</UiButton
          >
          <template v-if="settings">
            <AdministrationOrganization
              v-if="activeSection === 'organization'"
              :controller="administration"
            />
            <AdministrationPeople
              v-else-if="activeSection === 'users'"
              :controller="administration"
            />
            <AdministrationSignIn
              v-else-if="activeSection === 'domains'"
              :controller="administration"
            />
            <AdministrationPermissions
              v-else
              :task="
                route.query.task === 'create-role' ? 'create-role' : undefined
              "
              :settings="settings"
              :users="users"
              :roles="roles"
              :request="graphql"
              @manage-person="administration.openUser"
              @success="
                (notice: string) => {
                  failed = false;
                  message = notice;
                }
              "
              @failure="
                (notice: string) => {
                  failed = true;
                  message = notice;
                }
              "
              @settings-changed="
                run(
                  refresh,
                  '',
                  $t(
                    'administration.copy.your_change_was_saved_but_the_settings_could',
                  ),
                )
              "
              @people-changed="
                run(
                  refreshPeople,
                  '',
                  $t(
                    'administration.copy.your_change_was_saved_but_the_team_list',
                  ),
                )
              "
            />
          </template>
        </div>
      </div>
    </div>
    <AdministrationMemberAccess :controller="administration" />
    <UiDialog
      :open="!!pending"
      :title="pending?.title ?? ''"
      @close="pending = undefined"
      ><p class="text-sm leading-relaxed text-muted">
        {{ pending?.description }}
      </p>
      <p
        v-if="failed && message"
        role="alert"
        class="mt-4 rounded-lg border border-danger bg-danger-subtle p-3 text-sm text-danger"
      >
        {{ message }}
      </p>
      <template #footer
        ><UiButton
          type="button"
          variant="secondary"
          @click="pending = undefined"
          :loading="busy"
          >{{ $t("builder.config_modal.buttons.cancel") }}</UiButton
        ><UiButton
          type="button"
          @click="applyConfirmed"
          variant="primary"
          :loading="busy"
          >{{
            busy
              ? $t("administration.copy.applying")
              : $t("storage.upload.confirm")
          }}</UiButton
        ></template
      ></UiDialog
    >
  </section>
</template>
