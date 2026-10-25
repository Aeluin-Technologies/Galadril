<script setup lang="ts">
import { onClickOutside } from "@vueuse/core";
import {
  ArrowRightOnRectangleIcon,
  Cog6ToothIcon,
} from "@heroicons/vue/24/outline";
import { accountInitials } from "~/utils/account.mjs";

const { session, administrator, refreshAccount } = useAccount();
onMounted(async () => {
  try {
    await refreshAccount();
  } catch {
    session.value = null;
  }
});
const dropdown = ref<HTMLDetailsElement>();
const route = useRoute();
function closeMenu() {
  if (dropdown.value) dropdown.value.open = false;
}
onClickOutside(dropdown, closeMenu);
watch(() => route.fullPath, closeMenu);
const label = computed(() => session.value?.displayName || "Account");
</script>

<template>
  <details
    ref="dropdown"
    class="relative shrink-0"
    @keydown.esc.prevent="closeMenu"
  >
    <summary
      aria-label="Account menu"
      class="list-none cursor-pointer [&::-webkit-details-marker]:hidden flex h-8 w-8 items-center justify-center rounded-full border border-border bg-primary-subtle text-xs font-semibold text-primary-foreground transition hover:bg-primary-subtle focus-visible:outline-2 focus-visible:outline-focus focus-visible:outline-offset-2"
    >
      {{ accountInitials(session?.displayName) }}
    </summary>
    <div
      class="absolute right-0 z-50 mt-2 w-64 origin-top-right rounded-xl border border-border bg-surface p-1 shadow-lg focus:outline-none"
    >
      <div class="border-b border-border px-3 py-3 mb-1">
        <p class="truncate text-sm font-medium text-foreground">{{ label }}</p>
        <p class="mt-1 text-xs text-muted">
          {{ administrator ? "Organization administrator" : "Signed in" }}
        </p>
      </div>
      <NuxtLink
        to="/settings/user"
        class="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-foreground hover:bg-surface-muted focus-visible:bg-surface-muted"
      >
        <Cog6ToothIcon class="h-4 w-4 text-muted" />User settings
      </NuxtLink>
      <template v-if="administrator">
        <NuxtLink
          to="/settings/sso"
          aria-label="Administration"
          class="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-foreground hover:bg-surface-muted focus-visible:bg-surface-muted"
        >
          <Cog6ToothIcon class="h-4 w-4 text-muted" />Administration
        </NuxtLink>
      </template>
      <form action="/auth/logout" method="post">
        <UiButton
          type="submit"
          aria-label="Sign out"
          variant="ghost"
          class="flex w-full items-center gap-2"
        >
          <ArrowRightOnRectangleIcon class="h-4 w-4 text-muted" />Sign out
        </UiButton>
      </form>
    </div>
  </details>
</template>
