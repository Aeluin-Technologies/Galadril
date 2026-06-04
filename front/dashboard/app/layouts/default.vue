<script setup>
const isCommandOpen = ref(false);

const handleGlobalKeys = (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "k") {
    e.preventDefault();
    e.stopImmediatePropagation();
    isCommandOpen.value = !isCommandOpen.value;
  }
};

onMounted(() => {
  window.addEventListener("keydown", handleGlobalKeys);
});

onUnmounted(() => {
  window.removeEventListener("keydown", handleGlobalKeys);
});
</script>

<template>
  <div
    class="h-screen w-screen bg-slate-50 overflow-hidden flex flex-col font-sans antialiased text-slate-800"
  >
    <NavbarTop @open-command="isCommandOpen = true" class="flex-shrink-0" />

    <div class="flex flex-1 overflow-hidden relative">
      <main class="flex-1 relative bg-white overflow-hidden min-w-0">
        <slot />
      </main>
      <NavbarAction
        v-if="hasSelection"
        class="flex-shrink-0 border-l border-slate-200"
      />
      <AiChatDrawer />
    </div>

    <UtilsCommandBar :is-open="isCommandOpen" @close="isCommandOpen = false" />
  </div>
</template>
