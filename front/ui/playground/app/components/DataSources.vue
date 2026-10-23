<script setup lang="ts">
import { computed, ref } from "vue";
import type { Source } from "../types";
const props = defineProps<{ sources: readonly Source[] }>();
const search = ref("");
const status = ref("all");
const filtered = computed(() =>
  props.sources.filter(
    (source) =>
      (status.value === "all" || source.status === status.value) &&
      `${source.name} ${source.kind}`
        .toLowerCase()
        .includes(search.value.toLowerCase()),
  ),
);

function clear() {
  search.value = "";
  status.value = "all";
}
</script>

<template>
  <UiCard
    title="Data sources"
    description="Manage connections and keep your information up to date."
  >
    <template #actions
      ><UiBadge tone="neutral">{{ sources.length }} sources</UiBadge></template
    >
    <div class="mb-5 grid gap-4 sm:grid-cols-[minmax(0,1fr)_11rem]">
      <UiInput
        v-model="search"
        label="Search sources"
        type="search"
        placeholder="Search by name or type…"
      />
      <UiSelect
        v-model="status"
        label="Status"
        :options="[
          { value: 'all', label: 'All statuses' },
          { value: 'success', label: 'Connected' },
          { value: 'warning', label: 'Needs attention' },
        ]"
      />
    </div>
    <div
      v-if="filtered.length"
      class="overflow-x-auto rounded-control border border-border"
      role="region"
      aria-label="Data source connections"
      tabindex="0"
    >
      <table class="w-full text-start text-ui">
        <caption class="sr-only">
          Data source connections and current status
        </caption>
        <thead class="border-b border-border bg-canvas text-ui-meta text-muted">
          <tr>
            <th scope="col" class="px-4 py-3 text-start font-medium">Source</th>
            <th scope="col" class="px-4 py-3 text-start font-medium">Status</th>
            <th scope="col" class="px-4 py-3 text-start font-medium">
              Updated
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-border">
          <tr
            v-for="source in filtered"
            :key="source.name"
            class="transition-colors duration-fast hover:bg-canvas"
          >
            <th scope="row" class="px-4 py-5 text-start font-normal">
              <span class="block font-medium">{{ source.name }}</span
              ><span
                class="mt-1 block whitespace-nowrap text-ui-meta text-muted"
                >{{ source.kind }}</span
              >
            </th>
            <td class="whitespace-nowrap px-4 py-5">
              <UiBadge :tone="source.status">{{ source.statusLabel }}</UiBadge>
            </td>
            <td class="whitespace-nowrap px-4 py-5 text-ui-meta text-muted">
              {{ source.updated }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <UiEmptyState
      v-else
      title="No matching sources"
      description="Try a different name or remove a filter to see your connections."
      ><UiButton variant="secondary" @click="clear"
        >Clear filters</UiButton
      ></UiEmptyState
    >
    <template #footer
      ><div
        class="flex flex-wrap items-center justify-between gap-3 text-ui-meta text-muted"
      >
        <span>Demonstration data · changes stay in this browser</span
        ><span>Last check: 14:32 UTC</span>
      </div></template
    >
  </UiCard>
</template>
