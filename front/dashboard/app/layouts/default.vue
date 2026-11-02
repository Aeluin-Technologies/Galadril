<script setup>
const isCommandOpen = useState("global-command-bar", () => false);

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
    class="h-dvh w-full bg-canvas overflow-hidden flex flex-col font-sans antialiased text-foreground"
  >
    <NavbarTop @open-command="isCommandOpen = true" class="flex-shrink-0" />

    <div class="flex min-h-0 flex-1 overflow-hidden relative">
      <main
        class="min-h-0 flex-1 relative bg-surface overflow-auto min-w-0 overscroll-contain"
      >
        <slot />
      </main>
      <NavbarAction
        v-if="hasSelection"
        class="flex-shrink-0 border-l border-border"
      />
      <LazyAiChatDrawer />
    </div>

    <UtilsCommandBar :is-open="isCommandOpen" @close="isCommandOpen = false" />
    <AdministrationPasskeySetup />
  </div>
</template>

<style>
html,
body,
#__nuxt {
  height: 100%;
  min-height: 0;
  margin: 0;
}
body {
  overflow: hidden;
  overscroll-behavior: none;
}
</style>
