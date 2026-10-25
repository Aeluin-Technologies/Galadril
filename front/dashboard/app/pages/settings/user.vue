<script setup lang="ts">
import SettingsNavigation from "~/components/Settings/Navigation.vue";
const { t } = useI18n();
const route = useRoute();
const panel = ref<HTMLElement>();
const sections = computed(() => [
  {
    id: "personalization",
    label: t("personalization.title"),
    description: t("personalization.navigation"),
  },
  {
    id: "email",
    label: t("userSettings.email"),
    description: t("userSettings.emailDescription"),
  },
  {
    id: "oauth",
    label: t("userSettings.oauth"),
    description: t("userSettings.oauthDescription"),
  },
]);
const activeSection = computed(() =>
  route.hash === "#personalization"
    ? "personalization"
    : route.hash === "#oauth"
      ? "oauth"
      : "email",
);
watch(activeSection, () => nextTick(() => panel.value?.scrollTo({ top: 0 })));
useHead({
  title: () => t("administration.copy.user_settings_galadril"),
  meta: [{ name: "referrer", content: "no-referrer" }],
});
</script>

<template>
  <section
    ref="panel"
    :aria-label="$t('administration.copy.user_settings')"
    class="h-full min-h-0 overflow-y-auto bg-canvas"
  >
    <div class="mx-auto max-w-6xl px-5 py-8 sm:px-8 lg:py-10">
      <div class="flex flex-col gap-8 lg:flex-row lg:gap-12">
        <SettingsNavigation
          :label="$t('administration.copy.user_settings')"
          :sections="sections"
          :active="activeSection"
          base="/settings/user"
        />
        <div :id="activeSection" class="min-w-0 flex-1 space-y-6">
          <EmailWorkspace v-if="activeSection === 'email'" embedded />
          <AiPersonalInstructions
            v-else-if="activeSection === 'personalization'"
          />
          <template v-else>
            <UiPageHeader
              :title="$t('userSettings.oauth')"
              :description="$t('userSettings.oauthDescription')"
            />
            <p class="text-sm text-muted">
              {{
                $t(
                  "administration.copy.connecting_an_openai_account_is_optional_without_a",
                )
              }}
            </p>
            <AiOpenaiAccount />
          </template>
        </div>
      </div>
    </div>
  </section>
</template>
