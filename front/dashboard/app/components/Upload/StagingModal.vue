<script setup lang="ts">
import { CloudArrowUpIcon, DocumentIcon } from "@heroicons/vue/24/outline";
import { useS3Upload } from "~/composables/useS3Upload";
import { type IamPermission } from "~/composables/useIamPermissions";

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
const permissions = ref<IamPermission[]>([]);
const fileInputRef = ref<HTMLInputElement | null>(null);

watch(
  () => props.stagingData,
  (newVal) => {
    if (newVal) {
      targetName.value = newVal.originalName;
      selectedFile.value = null;
      permissions.value = [];
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
    permissions.value,
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
  <UiDialog
    :open="isOpen"
    :title="$t('storage.upload.modal_title')"
    @close="handleClose"
  >
    <div class="space-y-4 py-1">
      <p class="text-xs text-muted -mt-2 mb-2">
        {{ $t("storage.upload.staging_notice") }}
      </p>

      <div
        v-if="error"
        class="bg-danger-subtle border border-danger text-danger px-3 py-2.5 rounded-xl text-xs"
      >
        {{ error }}
      </div>

      <div
        v-if="!selectedFile"
        @click="fileInputRef?.click()"
        class="border-2 border-dashed border-border hover:border-focus bg-canvas/50 hover:bg-primary-subtle/20 rounded-2xl p-6 text-center cursor-pointer transition-all flex flex-col items-center group"
      >
        <CloudArrowUpIcon
          class="w-10 h-10 text-muted group-hover:text-primary-foreground transition-colors mb-2"
        />
        <span class="text-xs font-medium text-muted">{{
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
        class="bg-canvas p-3 rounded-xl border border-border flex items-center space-x-3"
      >
        <div class="p-2 bg-primary-subtle text-primary-foreground rounded-lg">
          <DocumentIcon class="w-5 h-5" />
        </div>
        <div class="flex-1 min-w-0">
          <label
            class="block text-[10px] font-bold uppercase tracking-wider text-muted mb-0.5"
            >{{ $t("storage.upload.final_key_label") }}</label
          >
          <UiInput v-model="targetName" type="text" class="w-full" />
        </div>
      </div>

      <UploadPermissionsManager v-if="selectedFile" v-model="permissions" />
    </div>

    <template #footer>
      <UiButton @click="handleClose" :disabled="isUploading" variant="ghost">
        {{ $t("actions.cancel") }}
      </UiButton>
      <UiButton
        @click="processFinalUpload"
        :disabled="!selectedFile || isUploading"
        variant="primary"
        class="flex items-center space-x-2"
      >
        <span
          v-if="isUploading"
          class="animate-spin rounded-full h-3.5 w-3.5 border-2 border-border border-t-transparent"
        />
        <span>{{
          isUploading
            ? $t("storage.upload.processing")
            : $t("storage.upload.confirm")
        }}</span>
      </UiButton>
    </template>
  </UiDialog>
</template>
