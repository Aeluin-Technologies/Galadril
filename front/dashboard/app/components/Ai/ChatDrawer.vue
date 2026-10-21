<script setup lang="ts">
import {
  XMarkIcon,
  ArrowTopRightOnSquareIcon,
  SparklesIcon,
} from "@heroicons/vue/24/outline";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { usePengoloChats } from "~/stores/usePengoloChats";
const ui = usePengoloUi();
const chats = usePengoloChats();
async function close() {
  await ui.closeTemporary();
  ui.drawerOpen = false;
}
async function expand() {
  ui.drawerOpen = false;
  await navigateTo(
    chats.active && !ui.temporary ? `/pengolo/${chats.active.id}` : "/pengolo",
  );
}
</script>
<template>
  <Transition name="p-pop"
    ><aside
      v-if="ui.drawerOpen"
      class="p-drawer"
      aria-label="Pengolo assistant"
    >
      <header class="p-drawer-header">
        <span><SparklesIcon />Pengolo</span>
        <div>
          <UiIconButton
            variant="ghost"
            type="button"
            @click="expand"
            :icon="ArrowTopRightOnSquareIcon"
            label="Open Pengolo workspace"
          /><UiIconButton
            variant="ghost"
            type="button"
            @click="close"
            :icon="XMarkIcon"
            label="Close Pengolo assistant"
          />
        </div>
      </header>
      <AiWorkspace compact view="chat" /></aside
  ></Transition>
</template>
<style scoped>
.p-drawer {
  position: fixed;
  right: 20px;
  bottom: 20px;
  width: min(480px, calc(100vw - 24px));
  height: min(720px, calc(100dvh - 90px));
  display: flex;
  flex-direction: column;
  border: 1px solid #dfd4c1;
  border-radius: 18px;
  overflow: hidden;
  background: #fcfbf8;
  box-shadow: 0 15px 60px #30241520;
  z-index: 55;
}
.p-drawer-header {
  height: 46px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px;
  border-bottom: 1px solid #e9e4db;
  color: #8f5d13;
  font-size: 12px;
}
.p-drawer-header > span,
.p-drawer-header > div {
  display: flex;
  align-items: center;
  gap: 7px;
}
.p-drawer-header svg {
  width: 16px;
  height: 16px;
}
.p-drawer .p-workspace {
  flex: 1;
  min-height: 0;
}
@media (max-width: 600px) {
  .p-drawer {
    right: 8px;
    bottom: 8px;
    width: calc(100vw - 16px);
    height: calc(100dvh - 80px);
  }
}
</style>
