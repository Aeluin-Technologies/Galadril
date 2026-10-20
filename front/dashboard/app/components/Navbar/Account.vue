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
      class="list-none cursor-pointer [&::-webkit-details-marker]:hidden flex h-8 w-8 items-center justify-center rounded-full border border-amber-200 bg-amber-50 text-xs font-semibold text-amber-800 transition hover:bg-amber-100 focus-visible:outline-2 focus-visible:outline-amber-600 focus-visible:outline-offset-2"
    >
      {{ accountInitials(session?.displayName) }}
    </summary>
    <div
      class="absolute right-0 z-50 mt-2 w-64 origin-top-right rounded-xl border border-zinc-200 bg-white p-1 shadow-lg focus:outline-none"
    >
      <div class="border-b border-zinc-100 px-3 py-3 mb-1">
        <p class="truncate text-sm font-medium text-zinc-900">{{ label }}</p>
        <p class="mt-1 text-xs text-zinc-500">
          {{ administrator ? "Organization administrator" : "Signed in" }}
        </p>
      </div>
      <NuxtLink
        to="/settings/user"
        class="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-zinc-700 hover:bg-stone-100 focus-visible:bg-stone-100"
      >
        <Cog6ToothIcon class="h-4 w-4 text-zinc-400" />User settings
      </NuxtLink>
      <template v-if="administrator">
        <NuxtLink
          to="/settings/sso"
          aria-label="Administration"
          class="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-zinc-700 hover:bg-stone-100 focus-visible:bg-stone-100"
        >
          <Cog6ToothIcon class="h-4 w-4 text-zinc-400" />Administration
        </NuxtLink>
      </template>
      <form action="/auth/logout" method="post">
        <button
          type="submit"
          aria-label="Sign out"
          class="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-zinc-700 hover:bg-stone-100 focus-visible:bg-stone-100"
        >
          <ArrowRightOnRectangleIcon class="h-4 w-4 text-zinc-400" />Sign out
        </button>
      </form>
    </div>
  </details>
</template>
