<script setup lang="ts">
import { ArrowUpTrayIcon, PencilSquareIcon } from "@heroicons/vue/24/outline";
import { prepareSignatureImage } from "~/utils/signature-ink";

defineProps<{ disabled: boolean }>();
const payload = defineModel<string>();
const fileInput = useTemplateRef<HTMLInputElement>("fileInput");
const method = ref<"draw" | "upload">("draw");
const strokes = ref<[number, number][][]>([]);
const uploaded =
  shallowRef<Awaited<ReturnType<typeof prepareSignatureImage>>>();
const uploading = ref(false);
const invalidImage = ref(false);
let revision = 0;

watchEffect(() => {
  payload.value =
    method.value === "draw"
      ? strokes.value.length &&
        strokes.value.every((stroke) => stroke.length >= 2)
        ? JSON.stringify(strokes.value)
        : undefined
      : uploaded.value?.payload;
});

async function upload(file: File | undefined) {
  if (!file) return;
  const own = ++revision;
  uploaded.value = undefined;
  invalidImage.value = false;
  uploading.value = true;
  try {
    const result = await prepareSignatureImage(file);
    if (own === revision) uploaded.value = result;
  } catch {
    if (own === revision) invalidImage.value = true;
  } finally {
    if (own === revision) uploading.value = false;
  }
}

function selectFile(event: Event) {
  const input = event.target;
  if (!(input instanceof HTMLInputElement)) return;
  void upload(input.files?.item(0) ?? undefined);
  input.value = "";
}

onBeforeUnmount(() => {
  revision++;
});
</script>

<template>
  <div class="grid min-w-0 gap-4 sm:grid-cols-[9rem_minmax(0,1fr)]">
    <div
      class="flex flex-wrap gap-1 sm:flex-col sm:border-r sm:border-border sm:pr-3"
      role="group"
      :aria-label="$t('signatureSettings.method')"
    >
      <UiNavigationLink>
        <button
          type="button"
          :aria-pressed="method === 'draw'"
          :disabled="disabled"
          class="aria-pressed:bg-primary-subtle aria-pressed:text-primary-foreground"
          @click="method = 'draw'"
        >
          <PencilSquareIcon aria-hidden="true" />{{
            $t("signatureSettings.drawMethod")
          }}
        </button>
      </UiNavigationLink>
      <UiNavigationLink>
        <button
          type="button"
          :aria-pressed="method === 'upload'"
          :disabled="disabled"
          class="aria-pressed:bg-primary-subtle aria-pressed:text-primary-foreground"
          @click="method = 'upload'"
        >
          <ArrowUpTrayIcon aria-hidden="true" />{{
            $t("signatureSettings.uploadMethod")
          }}
        </button>
      </UiNavigationLink>
    </div>
    <div v-if="method === 'draw'" class="min-w-0 space-y-3">
      <SettingsSignaturePad v-model="strokes" :disabled="disabled" />
      <div class="flex flex-wrap justify-end gap-2">
        <UiButton
          type="button"
          variant="ghost"
          :disabled="disabled || !strokes.length"
          @click="strokes = strokes.slice(0, -1)"
          >{{ $t("signatureSettings.undo") }}</UiButton
        >
        <UiButton
          type="button"
          variant="secondary"
          :disabled="disabled || !strokes.length"
          @click="strokes = []"
          >{{ $t("signatureSettings.clear") }}</UiButton
        >
      </div>
    </div>
    <UiFileDrop
      v-else
      :label="$t('signatureSettings.dropLabel')"
      :disabled="disabled || uploading"
      @files="upload($event[0])"
    >
      <div
        class="grid justify-items-center gap-3 rounded-panel border border-dashed border-border-control p-4 text-center"
      >
        <img
          v-if="uploaded"
          :src="uploaded.preview"
          :alt="$t('signatureSettings.preview')"
          class="max-h-36 max-w-full object-contain"
        />
        <p v-else class="text-sm text-muted">
          {{ $t("signatureSettings.uploadHelp") }}
        </p>
        <input
          ref="fileInput"
          type="file"
          hidden
          accept="image/png,image/jpeg"
          @change="selectFile"
        />
        <UiButton
          type="button"
          variant="secondary"
          :disabled="disabled || uploading"
          :loading="uploading"
          @click="fileInput?.click()"
        >
          <ArrowUpTrayIcon aria-hidden="true" />{{
            $t("signatureSettings.chooseImage")
          }}
        </UiButton>
        <UiAlert v-if="invalidImage" tone="danger">{{
          $t("signatureSettings.imageError")
        }}</UiAlert>
      </div>
    </UiFileDrop>
  </div>
</template>
