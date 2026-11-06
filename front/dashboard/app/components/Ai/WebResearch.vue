<script setup lang="ts">
import { webResearchSites } from "~/utils/pengolo-activity";
const props = defineProps<{
  events: readonly { kind: string; content: string }[];
}>();
const sites = computed(() =>
  webResearchSites(props.events).map((site) => ({
    ...site,
    host: new URL(site.origin).hostname,
  })),
);
const failed = reactive(new Set<string>());
</script>
<template>
  <div
    v-if="sites.length"
    class="my-2 flex items-center gap-2 text-sm text-muted"
    role="status"
  >
    <span class="inline-flex -space-x-2">
      <span
        v-for="site in sites.slice(0, 3)"
        :key="site.origin"
        class="relative inline-flex size-6 items-center justify-center overflow-hidden rounded-full border-2 border-surface bg-surface-muted text-xs"
        :title="site.origin"
        role="img"
        :aria-label="site.host"
      >
        <span>{{ site.host.charAt(0).toUpperCase() }}</span>
        <img
          v-if="!failed.has(site.origin)"
          :src="
            '/media/image?url=' +
            encodeURIComponent(
              site.origin.replace(/^http:/, 'https:') + '/favicon.ico',
            )
          "
          alt=""
          referrerpolicy="no-referrer"
          loading="lazy"
          class="absolute inset-0 size-full object-contain"
          @error="failed.add(site.origin)"
        />
      </span>
    </span>
    <span>{{ $t("pengolo.analysis.websites", { count: sites.length }) }}</span>
  </div>
</template>
