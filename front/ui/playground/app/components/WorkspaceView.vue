<script setup lang="ts">
import { ref } from "vue";
import {
  PencilSquareIcon,
  ArrowUpRightIcon,
  CircleStackIcon,
  CheckCircleIcon,
  ClockIcon,
} from "@heroicons/vue/24/outline";
import type { Tone } from "../../../src/types";
import type { Source } from "../types";

const sources: readonly Source[] = [
  {
    name: "Customer directory",
    detail: "Identity & organization",
    kind: "PostgreSQL",
    status: "success",
    statusLabel: "Connected",
    updated: "Just now",
  },
  {
    name: "Warehouse events",
    detail: "Logistics & field operations",
    kind: "Event stream",
    status: "warning",
    statusLabel: "Needs attention",
    updated: "8 min ago",
  },
  {
    name: "Document library",
    detail: "Knowledge & supporting files",
    kind: "Object storage",
    status: "success",
    statusLabel: "Connected",
    updated: "2 min ago",
  },
];
const notify = ref(true);
const name = ref("European operations");
const draft = ref("");
const error = ref("");
const renameOpen = ref(false);
const createOpen = ref(false);
const syncCount = ref(0);
const failureCount = ref(0);
const feedback = ref<{ title: string; tone: Tone } | null>(null);
function rename() {
  draft.value = name.value;
  error.value = "";
  renameOpen.value = true;
}
function save() {
  if (!draft.value.trim()) {
    error.value = "Enter a workspace name.";
    return;
  }
  name.value = draft.value.trim();
  renameOpen.value = false;
  feedback.value = { title: "Workspace renamed", tone: "success" };
}
/** Simulates a local request so the gallery demonstrates pending and result states. */
async function synchronize() {
  feedback.value = null;
  await new Promise<void>((resolve) => setTimeout(resolve, 800));
  syncCount.value++;
  feedback.value = { title: "Sources synchronized", tone: "success" };
}
async function fail() {
  failureCount.value++;
  throw new Error("Demonstration failure");
}
function failed() {
  feedback.value = {
    title: "The operation failed. Try again.",
    tone: "danger",
  };
}
defineExpose({
  synchronize,
  create: () => {
    createOpen.value = true;
  },
});
</script>

<template>
  <div class="space-y-6">
    <div class="grid gap-4 md:grid-cols-3">
      <UiCard
        ><div class="flex items-center justify-between text-muted">
          <h2 class="font-medium">Active sources</h2>
          <CircleStackIcon class="size-5" aria-hidden="true" />
        </div>
        <p class="mt-4 text-3xl font-semibold tabular-nums">
          03<span class="ms-2 text-ui font-normal text-muted"
            >/ 03 configured</span
          >
        </p>
        <p class="mt-3 flex items-center gap-2 text-ui-meta text-success">
          <CheckCircleIcon class="size-4" aria-hidden="true" />All sources
          available
        </p></UiCard
      >
      <UiCard
        ><div class="flex items-center justify-between text-muted">
          <h2 class="font-medium">Records processed</h2>
          <ArrowUpRightIcon class="size-5" aria-hidden="true" />
        </div>
        <p class="mt-4 text-3xl font-semibold tabular-nums">128,402</p>
        <p class="mt-3 text-ui-meta text-muted">
          <span class="font-semibold text-success">+12.8%</span> compared with
          yesterday
        </p></UiCard
      >
      <UiCard
        ><div class="flex items-center justify-between text-muted">
          <h2 class="font-medium">Last synchronization</h2>
          <ClockIcon class="size-5" aria-hidden="true" />
        </div>
        <p class="mt-4 text-3xl font-semibold">Just now</p>
        <p class="mt-3 text-ui-meta text-muted">
          Next automatic check in 5 minutes
        </p></UiCard
      >
    </div>
    <UiAlert v-if="feedback" :tone="feedback.tone" :title="feedback.title" />
    <div class="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_18rem]">
      <DataSources :sources="sources" />
      <div class="space-y-5">
        <UiCard title="Workspace" description="A shared space for your team.">
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <p class="text-ui-meta text-muted">Name</p>
              <h3 class="mt-1 break-words font-semibold">{{ name }}</h3>
            </div>
            <UiIconButton
              :icon="PencilSquareIcon"
              label="Rename workspace"
              @click="rename"
            />
          </div>
          <div class="my-5 border-t border-border" />
          <UiCheckbox
            v-model="notify"
            label="Notify me about failures"
            help="Receive a notification when a source needs attention."
          />
        </UiCard>
        <UiAlert tone="warning" title="One source needs attention"
          >Warehouse events has not sent data for 8 minutes. Check its
          connection.</UiAlert
        >
        <details class="rounded-panel border border-border bg-surface p-4">
          <summary
            class="cursor-pointer rounded-control text-ui-meta font-medium text-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus"
          >
            Interaction examples
          </summary>
          <div class="mt-4 space-y-3">
            <UiButton variant="danger" :action="fail" @action-error="failed"
              >Test an error</UiButton
            >
            <p class="text-ui-meta text-muted">
              Successful requests:
              <span data-testid="sync-count">{{ syncCount }}</span> · Error
              requests:
              <span data-testid="failure-count">{{ failureCount }}</span>
            </p>
          </div>
        </details>
      </div>
    </div>
    <UiDialog
      v-model:open="renameOpen"
      title="Rename workspace"
      description="Choose a clear name so your team can find this workspace."
    >
      <form id="rename-workspace" novalidate @submit.prevent="save">
        <UiInput
          v-model="draft"
          label="Workspace name"
          :error="error"
          autofocus
          required
          maxlength="80"
          @update:model-value="error = ''"
        />
      </form>
      <template #footer
        ><UiButton variant="secondary" @click="renameOpen = false"
          >Cancel</UiButton
        ><UiButton type="submit" form="rename-workspace"
          >Save changes</UiButton
        ></template
      >
    </UiDialog>
    <UiDialog
      v-model:open="createOpen"
      title="Connect a data source"
      description="Choose the connection type for your next source."
    >
      <UiAlert title="Demonstration environment"
        >This example uses local data. No connection will be created.</UiAlert
      >
      <template #footer
        ><UiButton variant="secondary" @click="createOpen = false"
          >Close</UiButton
        ></template
      >
    </UiDialog>
  </div>
</template>
