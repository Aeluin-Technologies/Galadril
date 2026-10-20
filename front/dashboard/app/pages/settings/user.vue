<script setup lang="ts">
useHead({ title: "User settings · Galadril" });
</script>

<template>
  <div class="mx-auto max-w-3xl space-y-8 px-6 py-10">
    <header>
      <h1 class="text-2xl font-semibold text-zinc-900">User settings</h1>
      <p class="mt-2 text-sm text-zinc-600">
        Manage your personal connections and preferences.
      </p>
    </header>
    <p class="text-sm text-zinc-600">
      Connecting an OpenAI account is optional. Without a connected account,
      conversations use the OpenAI API key configured for this installation.
    </p>
    <AiOpenaiAccount />
  </div>
</template>
