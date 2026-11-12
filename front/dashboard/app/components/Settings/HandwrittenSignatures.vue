<script setup lang="ts">
import {
  pengoloMutation,
  pengoloOperation,
  pengoloRequest,
} from "~/utils/pengolo-api";

interface Mark {
  id: string;
  label: string;
  createdAt: number;
}

type Point = [number, number];
const { session } = useAccount();
const { t } = useI18n();
const drawingForm = useId();
const drawing = ref(false);
const marks = shallowRef<Mark[]>();
const strokes = ref<Point[][]>([]);
const label = ref("");
const selected = shallowRef<{ id: string; paths: string[] }>();
const confirming = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");
let pending: AbortController | undefined;
let active = false;

function reset(): void {
  pending?.abort();
  pending = undefined;
  marks.value = undefined;
  drawing.value = false;
  strokes.value = [];
  label.value = "";
  selected.value = undefined;
  confirming.value = false;
  busy.value = false;
  error.value = "";
  notice.value = "";
}

async function request(
  action: (signal: AbortSignal) => Promise<() => void>,
): Promise<void> {
  if (busy.value) return;
  const controller = new AbortController();
  pending = controller;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const apply = await action(controller.signal);
    if (!controller.signal.aborted) apply();
  } catch {
    if (!controller.signal.aborted) error.value = t("signatureSettings.error");
  } finally {
    if (pending === controller) {
      pending = undefined;
      busy.value = false;
    }
  }
}

function load(): Promise<void> {
  return request(async (signal) => {
    const result = await pengoloRequest<Mark[]>(
      pengoloOperation("PengoloHandwrittenSignatures"),
      {},
      signal,
    );
    return () => {
      marks.value = result;
    };
  });
}

function openDrawing(): void {
  error.value = "";
  notice.value = "";
  drawing.value = true;
}

function closeDrawing(): void {
  drawing.value = false;
  label.value = "";
  strokes.value = [];
  error.value = "";
}

function save(): Promise<void> | undefined {
  if (
    !drawing.value ||
    !marks.value ||
    marks.value.length >= 8 ||
    !label.value.trim() ||
    !strokes.value.length
  )
    return;
  return request(async (signal) => {
    const mark = await pengoloMutation<Mark>(
      "createPengoloHandwrittenSignature",
      { label: label.value.trim(), trace: JSON.stringify(strokes.value) },
      signal,
    );
    return () => {
      marks.value = [mark, ...(marks.value ?? [])];
      closeDrawing();
      notice.value = t("signatureSettings.saved");
    };
  });
}

function preview(id: string): Promise<void> {
  return request(async (signal) => {
    const trace = await pengoloRequest<string | null>(
      pengoloOperation("PengoloHandwrittenSignatureTrace"),
      { id },
      signal,
    );
    if (!trace) throw new Error("Signature unavailable");
    const paths: Point[][] = JSON.parse(trace);
    return () => {
      selected.value = {
        id,
        paths: paths.map((stroke) =>
          stroke.map((point) => point.join(",")).join(" "),
        ),
      };
      confirming.value = false;
    };
  });
}

function closePreview(): void {
  selected.value = undefined;
  confirming.value = false;
  error.value = "";
}

function remove(): Promise<void> | undefined {
  const id = selected.value?.id;
  if (!id || !confirming.value) return;
  return request(async (signal) => {
    await pengoloRequest<boolean>(
      pengoloOperation("deletePengoloHandwrittenSignature"),
      { id },
      signal,
    );
    return () => {
      marks.value = marks.value?.filter((mark) => mark.id !== id);
      selected.value = undefined;
      confirming.value = false;
      notice.value = t("signatureSettings.removed");
    };
  });
}

function activate(): void {
  active = true;
  if (!marks.value && session.value) void load();
}

function deactivate(): void {
  active = false;
  reset();
}

watch(
  [() => session.value?.tenant, () => session.value?.subject],
  () => {
    reset();
    if (active && session.value) void load();
  },
  { flush: "sync" },
);

onMounted(() => {
  window.addEventListener("pengolo-access-invalidated", reset);
  activate();
});
onActivated(activate);
onDeactivated(deactivate);
onBeforeUnmount(() => {
  deactivate();
  window.removeEventListener("pengolo-access-invalidated", reset);
});
</script>

<template>
  <UiCard aria-labelledby="signature-settings-heading" class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 id="signature-settings-heading" class="text-lg font-semibold">
        {{ $t("signatureSettings.title") }}
      </h2>
      <UiButton
        v-if="marks && marks.length < 8"
        :disabled="busy"
        @click="openDrawing"
      >
        {{ $t("signatureSettings.add") }}
      </UiButton>
    </div>
    <p class="text-sm text-muted">{{ $t("signatureSettings.help") }}</p>
    <p v-if="!marks && busy" role="status">
      {{ $t("signatureSettings.loading") }}
    </p>
    <p v-if="marks?.length === 0" class="text-sm text-muted">
      {{ $t("signatureSettings.empty") }}
    </p>
    <ul
      v-if="marks?.length"
      class="divide-y divide-border rounded-panel border border-border"
    >
      <li v-for="mark in marks" :key="mark.id">
        <UiButton
          variant="secondary"
          :disabled="busy"
          class="w-full"
          align="start"
          @click="preview(mark.id)"
          >{{ mark.label }}</UiButton
        >
      </li>
    </ul>
    <UiDialog
      :open="!!selected"
      :title="marks?.find((mark) => mark.id === selected?.id)?.label ?? ''"
      :dismissible="!busy"
      @close="closePreview"
    >
      <template v-if="selected">
        <svg
          role="img"
          :aria-label="$t('signatureSettings.preview')"
          viewBox="0 0 1000 1000"
          preserveAspectRatio="none"
          class="h-32 w-full text-foreground"
        >
          <polyline
            v-for="(path, index) in selected.paths"
            :key="index"
            :points="path"
            vector-effect="non-scaling-stroke"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        <div v-if="confirming" class="flex flex-wrap items-center gap-2">
          <span>{{ $t("signatureSettings.confirmHelp") }}</span>
          <UiButton
            variant="secondary"
            :disabled="busy"
            @click="confirming = false"
            >{{ $t("actions.cancel") }}</UiButton
          >
          <UiButton variant="danger" :loading="busy" @click="remove">{{
            $t("signatureSettings.confirm")
          }}</UiButton>
        </div>
        <UiButton
          v-else
          variant="secondary"
          :disabled="busy"
          @click="confirming = true"
          >{{ $t("signatureSettings.remove") }}</UiButton
        >
        <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
      </template>
    </UiDialog>
    <UiDialog
      :open="drawing"
      :title="$t('signatureSettings.draw')"
      :close-label="$t('actions.close')"
      :dismissible="!busy"
      @close="closeDrawing"
    >
      <form
        v-if="drawing"
        :id="drawingForm"
        class="space-y-4"
        @submit.prevent="save"
      >
        <UiInput
          v-model="label"
          :label="$t('signatureSettings.label')"
          autocomplete="off"
          maxlength="60"
          :disabled="busy"
          autofocus
        />
        <SettingsSignaturePad v-model="strokes" :disabled="busy" />
        <div class="flex flex-wrap justify-end gap-2">
          <UiButton
            type="button"
            variant="ghost"
            :disabled="busy || !strokes.length"
            @click="strokes = strokes.slice(0, -1)"
            >{{ $t("signatureSettings.undo") }}</UiButton
          >
          <UiButton
            type="button"
            variant="secondary"
            :disabled="busy || !strokes.length"
            @click="strokes = []"
            >{{ $t("signatureSettings.clear") }}</UiButton
          >
        </div>
        <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
      </form>
      <template #footer>
        <UiButton variant="secondary" :disabled="busy" @click="closeDrawing">
          {{ $t("actions.cancel") }}
        </UiButton>
        <UiButton
          type="submit"
          :form="drawingForm"
          :loading="busy"
          :disabled="!label.trim() || !strokes.length"
          >{{ $t("signatureSettings.save") }}</UiButton
        >
      </template>
    </UiDialog>
    <p v-if="marks && marks.length >= 8" class="text-sm text-muted">
      {{ $t("signatureSettings.limit") }}
    </p>
    <UiAlert v-if="error && !drawing && !selected" tone="danger">{{
      error
    }}</UiAlert>
    <UiButton
      v-if="error && !marks"
      variant="secondary"
      :disabled="busy"
      @click="load"
      >{{ $t("signatureSettings.reload") }}</UiButton
    >
    <p v-if="notice" role="status" class="text-sm text-success">{{ notice }}</p>
  </UiCard>
</template>
