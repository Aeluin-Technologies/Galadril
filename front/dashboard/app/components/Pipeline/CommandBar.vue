<script setup lang="ts">
/** Native view tabs keep action-button live regions outside the ARIA tablist. */
import {
  PencilSquareIcon,
  EllipsisHorizontalIcon,
  ArrowDownTrayIcon,
  CheckBadgeIcon,
  CloudArrowUpIcon,
  RocketLaunchIcon,
  TrashIcon,
} from "@heroicons/vue/24/outline";
import type { MenuItem } from "@galadril/ui/types";
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
  download: () => Promise<void>;
}>();
const emit = defineEmits<{
  save: [];
  branch: [value: string | undefined];
  transition: [operation: "approve" | "publish" | "deploy"];
  remove: [];
  failure: [];
  email: [];
}>();
const renaming = ref(false);
const navigation = ref<HTMLElement>();
const { t } = useI18n();
const tabs = ["edit", "proposals", "history"] as const;
const unavailable = computed(
  () => props.busy || props.loading || props.dirty || !props.artifact,
);
const actions = computed<MenuItem[]>(() => [
  {
    id: "approve",
    label: t("pipeline.lifecycle.approve"),
    description: t("pipeline.chrome.approveHelp"),
    icon: CheckBadgeIcon,
    disabled:
      unavailable.value ||
      props.branch !== "main" ||
      !props.canApprove ||
      props.artifact?.lifecycle === "approved",
  },
  {
    id: "publish",
    label: t("pipeline.library.publish"),
    description: t("pipeline.chrome.publishHelp"),
    icon: CloudArrowUpIcon,
    disabled:
      unavailable.value ||
      props.branch !== "main" ||
      props.artifact?.lifecycle !== "approved",
  },
  {
    id: "deploy",
    label: t("pipeline.lifecycle.deploy"),
    icon: RocketLaunchIcon,
    disabled:
      unavailable.value ||
      props.branch !== "main" ||
      !props.artifact?.authoring?.release ||
      props.artifact.published_revision_id ===
        props.artifact.authoring.release.revision_id,
  },
  {
    id: "export",
    label: t("pipeline.composition.export"),
    icon: ArrowDownTrayIcon,
    disabled: props.busy,
  },
  { id: "email", label: t("pipeline.email.title"), disabled: !props.artifact },
  {
    id: "delete",
    label: t("pipeline.library.delete"),
    icon: TrashIcon,
    tone: "danger",
    disabled: props.busy || !props.artifact,
  },
]);
async function action(value: string) {
  if (value === "approve" || value === "publish" || value === "deploy")
    emit("transition", value);
  else if (value === "delete") emit("remove");
  else if (value === "email") emit("email");
  else if (value === "export") {
    try {
      await props.download();
    } catch {
      emit("failure");
    }
  }
}
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
          <h1 class="truncate text-xl font-semibold text-foreground">
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
        <UiInput
          v-if="renaming"
          v-model="name"
          :label="$t('pipeline.header.pipeline_name')"
          class="mt-2 max-w-md"
        />
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs text-muted" role="status">{{
          $t(dirty ? "pipeline.chrome.unsaved" : "pipeline.chrome.saved")
        }}</span
        ><UiButton
          :disabled="busy || loading || !dirty || !name.trim()"
          :loading="busy"
          @click="emit('save')"
          >{{ $t("pipeline.library.save") }}</UiButton
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
      <div class="flex items-center gap-2 pb-2">
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
