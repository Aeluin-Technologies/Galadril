<script setup lang="ts">
/** Native view tabs keep action-button live regions outside the ARIA tablist. */
import {
  PencilSquareIcon,
  EllipsisHorizontalIcon,
  DocumentCheckIcon,
  CloudArrowUpIcon,
  RocketLaunchIcon,
  TrashIcon,
} from "@heroicons/vue/24/outline";
import type { MenuItem } from "@galadril/ui/types";
import { primaryPipelineAction } from "~/utils/pipeline";
import type { PipelineArtifact } from "~/utils/pipeline";
const name = defineModel<string>({ required: true });
const view = defineModel<"edit" | "proposals" | "history">("view", {
  required: true,
});
const props = defineProps<{
  artifact?: PipelineArtifact;
  branch: string;
  dirty: boolean;
  busy: boolean;
  loading: boolean;
  canApprove: boolean;
}>();
const emit = defineEmits<{
  save: [];
  branch: [value: string | undefined];
  transition: [operation: "publish" | "deploy"];
  remove: [];
  failure: [];
}>();
const renaming = ref(false);
const navigation = ref<HTMLElement>();
const { t } = useI18n();
const tabs = ["edit", "proposals", "history"] as const;
const primary = computed(() =>
  primaryPipelineAction(props.dirty, props.artifact?.lifecycle, props.branch),
);
const primaryIcon = computed(() =>
  primary.value === "save"
    ? DocumentCheckIcon
    : primary.value === "publish"
      ? CloudArrowUpIcon
      : RocketLaunchIcon,
);
const primaryLabel = computed(() =>
  t(
    primary.value === "save"
      ? "pipeline.library.save"
      : primary.value === "publish"
        ? "pipeline.library.publish"
        : primary.value === "deploy"
          ? "pipeline.lifecycle.deploy"
          : "pipeline.lifecycle.states.deployed",
  ),
);
const actions = computed<MenuItem[]>(() => [
  {
    id: "save",
    label: t("pipeline.library.save"),
    icon: DocumentCheckIcon,
    disabled: !props.dirty || props.busy,
  },
  {
    id: "delete",
    label: t("pipeline.library.delete"),
    icon: TrashIcon,
    tone: "danger",
    disabled: props.busy || !props.artifact,
  },
]);
function action(value: string) {
  if (value === "delete") emit("remove");
  if (value === "save") emit("save");
}
function proceed() {
  if (primary.value === "save") emit("save");
  else if (primary.value === "publish" || primary.value === "deploy")
    emit("transition", primary.value);
}
const titleEditor = ref<{ focus: () => void }>();
watch(renaming, async (value) => {
  if (value) {
    await nextTick();
    titleEditor.value?.focus();
  }
});
function keyboard(event: KeyboardEvent) {
  let index = tabs.indexOf(view.value);
  if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
  else if (event.key === "ArrowLeft")
    index = (index + tabs.length - 1) % tabs.length;
  else if (event.key === "Home") index = 0;
  else if (event.key === "End") index = tabs.length - 1;
  else return;
  event.preventDefault();
  view.value = tabs[index] ?? "edit";
  nextTick(() =>
    navigation.value
      ?.querySelector<HTMLButtonElement>(`[data-view="${view.value}"]`)
      ?.focus(),
  );
}
</script>
<template>
  <header class="shrink-0 border-b border-border bg-surface px-4 pt-3 sm:px-6">
    <div class="flex min-w-0 flex-wrap items-center justify-between gap-3 pb-3">
      <div class="min-w-0 flex-1">
        <NuxtLink to="/pipeline" class="text-xs text-muted hover:text-info">{{
          $t("pipeline.library.back")
        }}</NuxtLink>
        <div class="flex min-w-0 items-center gap-2">
          <UiInput
            v-if="renaming"
            ref="titleEditor"
            v-model="name"
            :aria-label="$t('pipeline.header.pipeline_name')"
            class="min-w-0 max-w-lg flex-1"
            @keydown.enter="renaming = false"
            @keydown.esc="renaming = false"
            @focusout="renaming = false"
          />
          <h1 v-else class="truncate text-xl font-semibold text-foreground">
            {{ name || $t("pipeline.composition.new_pipeline") }}
          </h1>
          <UiIconButton
            :icon="PencilSquareIcon"
            :label="$t('pipeline.chrome.rename')"
            size="sm"
            variant="ghost"
            @click="renaming = !renaming"
          /><UiBadge
            v-if="artifact"
            :tone="artifact.lifecycle === 'deployed' ? 'success' : 'neutral'"
            >{{
              $t(
                `pipeline.lifecycle.states.${branch === "main" ? (artifact.lifecycle ?? "draft") : "draft"}`,
              )
            }}</UiBadge
          >
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs text-muted" role="status">{{
          $t(dirty ? "pipeline.chrome.unsaved" : "pipeline.chrome.saved")
        }}</span
        ><UiButton
          :disabled="
            busy ||
            loading ||
            !name.trim() ||
            primary === 'deployed' ||
            (primary === 'save' && !dirty) ||
            (primary === 'publish' && !canApprove)
          "
          :loading="busy"
          @click="proceed"
          ><template #leading
            ><component :is="primaryIcon" class="h-4 w-4" /></template
          >{{ primaryLabel }}</UiButton
        ><UiMenu
          :label="$t('pipeline.chrome.actions')"
          :items="actions"
          variant="secondary"
          :icon-only="true"
          :trigger-icon="EllipsisHorizontalIcon"
          @select="action"
        />
      </div>
    </div>
    <div class="flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
      <div
        ref="navigation"
        role="tablist"
        :aria-label="$t('pipeline.chrome.views')"
        class="flex gap-1"
        @keydown="keyboard"
      >
        <button
          v-for="tab in tabs"
          :id="`pipeline-tab-${tab}`"
          :key="tab"
          role="tab"
          :data-view="tab"
          :aria-selected="view === tab"
          :aria-controls="`pipeline-panel-${tab}`"
          :tabindex="view === tab ? 0 : -1"
          type="button"
          class="h-10 border-b-2 px-4 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-focus"
          :class="
            view === tab
              ? 'border-primary text-foreground'
              : 'border-transparent text-muted'
          "
          @click="view = tab"
        >
          {{ $t(`pipeline.chrome.${tab}`) }}
        </button>
      </div>
      <div
        v-if="
          artifact && Object.keys(artifact.authoring?.branches ?? {}).length
        "
        class="flex items-center gap-2 pb-2"
      >
        <span class="text-xs text-muted">{{
          $t("pipeline.collaboration.branch")
        }}</span
        ><UiSelect
          :model-value="branch"
          :aria-label="$t('pipeline.collaboration.branch')"
          :disabled="dirty || busy || !artifact"
          :options="
            ['main', ...Object.keys(artifact?.authoring?.branches ?? {})].map(
              (value) => ({ value, label: value }),
            )
          "
          @update:model-value="emit('branch', $event)"
        />
      </div>
    </div>
  </header>
</template>
