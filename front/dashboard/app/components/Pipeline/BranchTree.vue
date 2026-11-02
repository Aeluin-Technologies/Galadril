<script setup lang="ts">
import { ChevronRightIcon, ChevronDownIcon } from "@heroicons/vue/24/outline";
import type { AuthoringState } from "~/utils/pipeline";
const props = defineProps<{
  branches: AuthoringState["branches"];
  selected: string;
  disabled?: boolean;
  pipelineId?: string;
}>();
const emit = defineEmits<{ checkout: [branch: string] }>();
const collapsed = ref<string[]>([]);
const rows = computed(() => {
  const children = new Map<string, string[]>();
  const unknown: string[] = [];
  for (const [id, branch] of Object.entries(props.branches)) {
    const parent = branch.parent_branch;
    if (!parent || (parent !== "main" && !props.branches[parent])) {
      unknown.push(id);
      continue;
    }
    children.set(parent, [...(children.get(parent) ?? []), id]);
  }
  const result: Array<{
    id: string;
    depth: number;
    children: boolean;
    unknown: boolean;
  }> = [];
  const seen = new Set<string>();
  function visit(id: string, depth: number, legacy = false) {
    if (seen.has(id)) return;
    seen.add(id);
    result.push({
      id,
      depth,
      children: !!children.get(id)?.length,
      unknown: legacy,
    });
    if (!collapsed.value.includes(id))
      for (const child of children.get(id) ?? []) visit(child, depth + 1);
  }
  visit("main", 0);
  for (const id of unknown) visit(id, 0, true);
  // Invalid or cyclic legacy metadata must never recurse indefinitely or hide a branch.
  for (const id of Object.keys(props.branches)) {
    let ancestor: string | undefined = id;
    const chain = new Set<string>();
    let hidden = false;
    while (ancestor && !chain.has(ancestor)) {
      chain.add(ancestor);
      ancestor = props.branches[ancestor]?.parent_branch;
      if (ancestor && collapsed.value.includes(ancestor)) {
        hidden = true;
        break;
      }
    }
    if (!seen.has(id) && !hidden) visit(id, 0, true);
  }
  return result;
});
function toggle(id: string) {
  collapsed.value = collapsed.value.includes(id)
    ? collapsed.value.filter((value) => value !== id)
    : [...collapsed.value, id];
}
</script>
<template>
  <nav :aria-label="$t('pipeline.review.branchTree')">
    <ul class="space-y-1">
      <li
        v-for="row in rows"
        :key="row.id"
        class="flex min-w-0 items-center gap-1"
        :style="{ paddingInlineStart: `${row.depth * 18}px` }"
      >
        <UiIconButton
          v-if="row.children"
          :icon="
            collapsed.includes(row.id) ? ChevronRightIcon : ChevronDownIcon
          "
          :label="$t('pipeline.review.toggleBranch', { name: row.id })"
          :aria-expanded="!collapsed.includes(row.id)"
          size="sm"
          variant="ghost"
          @click="toggle(row.id)"
        />
        <span v-else class="w-7 shrink-0" aria-hidden="true">⑂</span>
        <NuxtLink
          v-if="pipelineId"
          :to="{
            path: `/pipeline/${encodeURIComponent(pipelineId)}`,
            query: { branch: row.id },
          }"
          class="truncate rounded-control px-2 py-1 text-sm text-primary-foreground hover:bg-primary-subtle"
          >{{ row.id }}</NuxtLink
        >
        <UiButton
          v-else
          variant="ghost"
          align="start"
          size="sm"
          :disabled="disabled"
          :aria-pressed="row.id === selected"
          @click="emit('checkout', row.id)"
          >{{ row.id }}</UiButton
        >
        <span v-if="row.unknown" class="text-xs text-muted">{{
          $t("pipeline.review.unknownOrigin")
        }}</span>
      </li>
    </ul>
  </nav>
</template>
