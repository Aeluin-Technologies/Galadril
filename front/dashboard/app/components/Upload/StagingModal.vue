<script setup lang="ts">
import { CloudArrowUpIcon, DocumentIcon } from "@heroicons/vue/24/outline";
import { useS3Upload } from "~/composables/useS3Upload";

const props = defineProps({
  isOpen: Boolean,
  stagingData: {
    type: Object as () => {
      uploadUrl: string;
      stagingKey: string;
      originalName: string;
    } | null,
    default: null,
  },
});
const emit = defineEmits(["close", "success"]);

const { uploadToS3Presigned, completeUpload, isUploading, error } =
  useS3Upload();

const selectedFile = ref<File | null>(null);
const targetName = ref("");
const fileInputRef = ref<HTMLInputElement | null>(null);

watch(
  () => props.stagingData,
  (newVal) => {
    if (newVal) {
      targetName.value = newVal.originalName;
      selectedFile.value = null;
    }
  },
);

function handleFileChange(e: Event) {
  const files = (e.target as HTMLInputElement).files;
  if (files && files.length > 0) {
    selectedFile.value = files[0] || null;
  }
}

async function processFinalUpload() {
  if (!selectedFile.value || !props.stagingData) return;

  isUploading.value = true;
  error.value = null;

  const s3Success = await uploadToS3Presigned(
    props.stagingData.uploadUrl,
    selectedFile.value,
  );
  if (!s3Success) {
    isUploading.value = false;
    return;
  }

  const finalDestKey = await completeUpload(
    props.stagingData.stagingKey,
    targetName.value.trim(),
  );

  isUploading.value = false;
  if (finalDestKey) {
    emit("success", finalDestKey);
    handleClose();
  }
}

function handleClose() {
  selectedFile.value = null;
  error.value = null;
  emit("close");
}
</script>

<template>
  <UtilsModal
    :is-open="isOpen"
    :title="$t('storage.upload.modal_title')"
    @close="handleClose"
  >
    <div class="space-y-4 py-1">
      <p class="text-xs text-slate-400 -mt-2 mb-2">
        {{ $t("storage.upload.staging_notice") }}
      </p>

      <div
        v-if="error"
        class="bg-red-50 border border-red-200 text-red-800 px-3 py-2.5 rounded-xl text-xs"
      >
        {{ error }}
      </div>

      <div
        v-if="!selectedFile"
        @click="fileInputRef?.click()"
        class="border-2 border-dashed border-slate-200 hover:border-amber-400 bg-slate-50/50 hover:bg-amber-50/20 rounded-2xl p-6 text-center cursor-pointer transition-all flex flex-col items-center group"
      >
        <CloudArrowUpIcon
          class="w-10 h-10 text-slate-300 group-hover:text-amber-500 transition-colors mb-2"
        />
        <span class="text-xs font-medium text-slate-600">{{
          $t("storage.upload.drag_drop")
        }}</span>
        <input
          ref="fileInputRef"
          type="file"
          class="hidden"
          @change="handleFileChange"
        />
      </div>

      <div
        v-else
        class="bg-slate-50 p-3 rounded-xl border border-slate-200 flex items-center space-x-3"
      >
        <div class="p-2 bg-amber-100 text-amber-700 rounded-lg">
          <DocumentIcon class="w-5 h-5" />
        </div>
        <div class="flex-1 min-w-0">
          <label
            class="block text-[10px] font-bold uppercase tracking-wider text-slate-400 mb-0.5"
            >{{ $t("storage.upload.final_key_label") }}</label
          >
          <input
            v-model="targetName"
            type="text"
            class="w-full text-xs font-medium text-slate-800 bg-white border border-slate-200 rounded-md px-2 py-1 outline-none focus:ring-1 focus:ring-amber-500"
          />
        </div>
      </div>
    </div>

    <template #footer>
      <button
        @click="handleClose"
        :disabled="isUploading"
        class="px-4 py-2 text-sm font-medium text-zinc-600 hover:text-zinc-800 disabled:opacity-50 transition-colors"
      >
        {{ $t("actions.cancel") }}
      </button>
      <button
        @click="processFinalUpload"
        :disabled="!selectedFile || isUploading"
        class="px-4 py-2 text-sm font-medium bg-amber-500 hover:bg-amber-600 disabled:bg-stone-100 text-white disabled:text-zinc-400 rounded-lg transition-colors flex items-center space-x-2 shadow-sm"
      >
        <span
          v-if="isUploading"
          class="animate-spin rounded-full h-3.5 w-3.5 border-2 border-white border-t-transparent"
        />
        <span>{{
          isUploading
            ? $t("storage.upload.processing")
            : $t("storage.upload.confirm")
        }}</span>
      </button>
    </template>
  </UtilsModal>
</template>
