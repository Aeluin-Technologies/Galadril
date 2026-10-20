<script setup lang="ts">
import { usePengoloOpenai } from "~/stores/usePengoloOpenai";
const account = usePengoloOpenai();
onMounted(() => {
  void account.initialize();
});
onBeforeUnmount(() => account.stop());
</script>

<template>
  <section
    aria-labelledby="chatgpt-heading"
    class="rounded-2xl border border-zinc-200 bg-white p-6 space-y-5"
  >
    <h2 id="chatgpt-heading" class="text-lg font-semibold">
      OpenAI connection
    </h2>
    <p v-if="!account.status && !account.error" role="status">
      Loading your connection settings…
    </p>
    <p v-if="account.status && !account.status.enabled">
      ChatGPT sign-in is disabled for this installation. Conversations use the
      configured OpenAI API key.
    </p>
    <p v-if="account.error" role="alert">{{ account.error }}</p>
    <template v-if="account.status?.enabled">
      <p>
        Eligible requests use your shared Codex / ChatGPT Work quota. Connecting
        does not add an allowance. Models depend on the selected account.
      </p>
      <p>
        Galadril can continue with its configured OpenAI API credits when your
        plan cannot serve a request. The conversation shows when API credits are
        used.
      </p>
      <p v-if="account.status?.quotaExhausted" role="status">
        ChatGPT usage limit reached. Manage your limit or try your plan again.
      </p>
      <p v-if="account.notice" role="status">{{ account.notice }}</p>
      <ul v-if="account.status?.accounts.length" class="p-openai-accounts">
        <li v-for="item in account.status.accounts" :key="item.id">
          <strong>{{ item.label }}</strong>
          <span>{{
            item.connected
              ? account.status.selected === item.id
                ? "Selected"
                : "Connected"
              : "Reconnect required"
          }}</span>
          <UtilsButton
            v-if="!item.connected"
            variant="ghost"
            type="button"
            :disabled="account.busy"
            :aria-label="`Reconnect ${item.label}`"
            @click="account.update('CONNECT', item.id)"
            >Reconnect</UtilsButton
          >
          <UtilsButton
            v-if="item.connected && account.status.selected !== item.id"
            variant="ghost"
            type="button"
            :disabled="account.busy"
            :aria-label="`Select ${item.label}`"
            @click="account.update('SELECT', item.id)"
            >Use account</UtilsButton
          >
          <UtilsButton
            variant="ghost"
            type="button"
            :disabled="account.busy"
            :aria-label="`Disconnect ${item.label}`"
            @click="account.update('DISCONNECT', item.id)"
            >Disconnect</UtilsButton
          >
        </li>
      </ul>
      <div class="flex flex-wrap items-center gap-3">
        <a
          href="https://chatgpt.com/settings/usage"
          target="_blank"
          rel="noopener noreferrer"
          >Manage usage</a
        >
        <UtilsButton
          v-if="account.status?.quotaExhausted"
          variant="ghost"
          type="button"
          :disabled="account.busy"
          @click="account.update('RETRY')"
          >Try ChatGPT plan again</UtilsButton
        >
        <UtilsButton
          type="button"
          :disabled="
            account.busy || (account.status?.accounts.length ?? 0) >= 4
          "
          @click="account.update('CONNECT')"
          >Continue with ChatGPT</UtilsButton
        >
      </div>
    </template>
  </section>
</template>

<style scoped>
.p-openai-accounts {
  list-style: none;
  padding: 0;
}
.p-openai-accounts li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.65rem;
  padding: 0.75rem 0;
  border-bottom: 1px solid var(--border-color, #d1d5db);
}
.p-openai-accounts span {
  font-size: 0.85rem;
  opacity: 0.75;
}
</style>
