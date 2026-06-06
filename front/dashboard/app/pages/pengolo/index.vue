<script setup>
import { useAiChatStore } from "~/stores/useAiChat";

const store = useAiChatStore();
const router = useRouter();

onMounted(() => {
  if (store.sessions && store.sessions.length > 0) {
    const targetId = store.activeSessionId || store.sessions[0].id;
    router.replace(`/pengolo/${targetId}`);
  } else {
    const newId = store.createNewSession("New Conversation");
    router.replace(`/pengolo/${newId}`);
  }
});
</script>

<template>
  <div class="flex h-screen w-screen bg-stone-50 items-center justify-center">
    <div class="flex flex-col items-center space-y-3">
      <div
        class="w-6 h-6 border-2 border-zinc-900 border-t-transparent rounded-full animate-spin"
      />
    </div>
  </div>
</template>
