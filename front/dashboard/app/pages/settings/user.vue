<script setup lang="ts">
const { t } = useI18n();

useHead({ title: t("administration.copy.user_settings_galadril") });
</script>

<template>
  <div class="mx-auto max-w-3xl space-y-8 px-6 py-10">
    <header>
      <h1 class="text-2xl font-semibold text-foreground">
        {{ $t("administration.copy.user_settings") }}
      </h1>
      <p class="mt-2 text-sm text-muted">
        {{
          $t(
            "administration.copy.manage_your_personal_connections_and_preferences",
          )
        }}
      </p>
    </header>
    <p class="text-sm text-muted">
      {{
        $t(
          "administration.copy.connecting_an_openai_account_is_optional_without_a",
        )
      }}
    </p>
    <AiOpenaiAccount />
  </div>
</template>
