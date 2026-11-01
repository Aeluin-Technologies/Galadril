<script setup lang="ts">
import {
  HomeIcon,
  ChevronRightIcon,
  SparklesIcon,
} from "@heroicons/vue/24/outline";
import { useAiChatStore } from "~/stores/useAiChat";

const emit = defineEmits(["open-command"]);

const route = useRoute();
const { t, te } = useI18n();
const chatStore = useAiChatStore();

interface Breadcrumb {
  label: string;
  path: string;
  isLast: boolean;
}

const breadcrumbs = computed<Breadcrumb[]>(() => {
  const pathNodes = route.path.split("/").filter((node) => node !== "");
  return pathNodes.map((node, index) => {
    const path =
      node === "settings"
        ? "/settings/sso"
        : "/" + pathNodes.slice(0, index + 1).join("/");
    const i18nKey = `routes.${node}`;
    const label =
      node === "settings"
        ? "Administration"
        : node === "sso"
          ? "Organization"
          : te(i18nKey)
            ? t(i18nKey)
            : node
                .replace(/-/g, " ")
                .replace(/\b\w/g, (char) => char.toUpperCase());

    return { label, path, isLast: index === pathNodes.length - 1 };
  });
});
</script>

<template>
  <nav
    class="flex h-14 items-center justify-between gap-2 border-b border-border bg-surface px-4 text-foreground relative z-[60]"
  >
    <div class="flex items-center space-x-1 text-sm font-medium">
      <NuxtLink
        :aria-label="$t('navbar.brand')"
        to="/"
        class="flex items-center hover:bg-surface-muted px-2 py-1.5 rounded-lg cursor-pointer transition group"
      >
        <HomeIcon
          class="w-4 h-4 mr-2 text-muted group-hover:text-primary-foreground"
        />
        <span class="hidden whitespace-nowrap text-foreground sm:inline">{{
          $t("navbar.brand")
        }}</span>
      </NuxtLink>

      <template v-for="crumb in breadcrumbs" :key="crumb.path">
        <ChevronRightIcon class="hidden md:block w-3.5 h-3.5 text-muted" />
        <NuxtLink
          v-if="!crumb.isLast || crumb.path === '/pipeline'"
          :to="crumb.path"
          class="hidden md:inline rounded-control px-2 py-1.5 text-primary-foreground hover:bg-primary-subtle"
          >{{ crumb.label }}</NuxtLink
        >
        <span
          v-else
          class="hidden md:inline px-2 py-1.5 text-muted font-normal"
          >{{ crumb.label }}</span
        >
      </template>
    </div>

    <div class="flex items-center gap-1.5 sm:gap-3">
      <UiSearchTrigger
        data-tour="search"
        @click="emit('open-command')"
        :label="$t('navbar.search.placeholder')"
        :shortcut="$t('navbar.search.shortcut')"
        aria-keyshortcuts="Control+K Meta+K"
        class="w-9 sm:w-64 lg:w-72"
      />

      <UiButton
        data-tour="copilot"
        @click="chatStore.toggleChat"
        variant="ghost"
        :aria-pressed="chatStore.isChatOpen"
        aria-label="Pengolo"
      >
        <SparklesIcon class="w-4 h-4 text-primary-foreground" />
        <span class="hidden sm:inline">Pengolo</span>
      </UiButton>
      <NavbarAccount />
    </div>
  </nav>
</template>
