<script setup lang="ts">
import {
  DocumentTextIcon,
  PhotoIcon,
  TableCellsIcon,
  EllipsisHorizontalIcon,
  EyeIcon,
  ArrowDownTrayIcon,
  TrashIcon,
  ArrowPathIcon,
  CheckCircleIcon,
} from "@heroicons/vue/24/outline";
import type { FileRecord } from "~/utils/pengolo-api";
import type { MenuItem } from "@galadril/ui/types";
import { indexingState } from "~/utils/pengolo";
const props = defineProps<{
  file: FileRecord;
  selected: boolean;
  picker?: boolean;
  busy?: boolean;
  canDelete: boolean;
  canIndex: boolean;
}>();
const emit = defineEmits<{
  select: [checked: boolean];
  attach: [];
  preview: [];
  download: [];
  remove: [];
  reindex: [];
}>();
const { t } = useI18n();
const progress = computed(() => indexingState(props.file.data));
const icon = computed(() =>
  props.file.data.mime.startsWith("image/")
    ? PhotoIcon
    : props.file.data.mime === "text/csv"
      ? TableCellsIcon
      : DocumentTextIcon,
);
const items = computed<MenuItem[]>(() => [
  {
    id: "preview",
    label: t("documents.preview"),
    icon: EyeIcon,
    disabled: props.file.data.status !== "ready",
  },
  {
    id: "download",
    label: t("documents.download"),
    icon: ArrowDownTrayIcon,
    disabled: props.file.data.status !== "ready",
  },
  ...(props.canIndex
    ? [
        {
          id: "reindex",
          label: t("documents.reindex"),
          icon: ArrowPathIcon,
          disabled: progress.value.active,
        },
      ]
    : []),
  ...(props.canDelete
    ? [
        {
          id: "remove",
          label: t("documents.delete"),
          icon: TrashIcon,
          tone: "danger" as const,
        },
      ]
    : []),
]);
function action(id: string) {
  if (id === "preview") emit("preview");
  else if (id === "download") emit("download");
  else if (id === "remove") emit("remove");
  else if (id === "reindex") emit("reindex");
}
</script>
<template>
  <li
    :class="[
      'flex min-w-0 items-center gap-3 border-b border-border px-3 py-4',
      selected ? 'bg-primary-subtle' : 'bg-surface',
    ]"
  >
    <UiCheckbox
      v-if="!picker"
      :model-value="selected"
      :aria-label="$t('documents.selectFile', { name: file.data.name })"
      :disabled="busy"
      @update:model-value="emit('select', $event === true)"
    />
    <component
      :is="icon"
      class="size-6 shrink-0 text-muted"
      aria-hidden="true"
    />
    <div class="min-w-0 flex-1 space-y-1">
      <p class="truncate font-medium" :title="file.data.name">
        {{ file.data.name }}
      </p>
      <p class="text-xs text-muted">
        {{ (file.data.size / 1024).toFixed(0) }} KB<span v-if="file.expiresAt">
          ·
          {{
            $t("documents.expires", {
              date: new Date(file.expiresAt).toLocaleDateString(),
            })
          }}</span
        >
      </p>
      <p
        v-if="progress.stage === 'completed'"
        class="flex items-center gap-1 text-xs text-success"
      >
        <CheckCircleIcon class="size-4" aria-hidden="true" />{{
          $t("documents.indexed")
        }}
      </p>
      <div v-else class="grid max-w-sm gap-1 text-xs text-muted">
        <span
          >{{ progress.label
          }}<template v-if="progress.active">
            · {{ progress.percent }}%</template
          ></span
        >
        <progress
          v-if="progress.active"
          :value="progress.percent"
          max="100"
          :aria-label="$t('documents.progress', { name: file.data.name })"
          class="h-1 w-full accent-primary-active"
        />
        <p v-if="progress.reasonKey" class="text-danger">
          {{ $t(progress.reasonKey) }}
        </p>
      </div>
    </div>
    <UiButton
      v-if="picker"
      variant="secondary"
      :disabled="file.data.status !== 'ready'"
      @click="emit('attach')"
      >{{ $t("documents.attach") }}</UiButton
    >
    <UiMenu
      v-else
      :label="$t('documents.actionsFor', { name: file.data.name })"
      icon-only
      :trigger-icon="EllipsisHorizontalIcon"
      variant="ghost"
      :disabled="busy"
      :items="items"
      @select="action"
    />
  </li>
</template>
