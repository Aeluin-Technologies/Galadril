<script setup lang="ts">
import type { EmailAttachment } from "~/utils/email";

const attachments = defineModel<EmailAttachment[]>({ required: true });
const reading = defineModel<boolean>("reading", { required: true });
const props = defineProps<{ disabled?: boolean }>();
const { t } = useI18n();
const error = ref("");
const maxFile = 8 * 1024 * 1024;
const maxEncoded = 14 * 1024 * 1024;
let reader: FileReader | undefined;

function read(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    reader = new FileReader();
    reader.onload = () => {
      const result = reader?.result;
      if (typeof result === "string")
        resolve(result.slice(result.indexOf(",") + 1));
      else reject(new Error("Attachment read failed"));
    };
    reader.onerror = reader.onabort = () =>
      reject(new Error("Attachment read interrupted"));
    reader.readAsDataURL(file);
  });
}
function abort() {
  const pending = reader;
  reader = undefined;
  pending?.abort();
  reading.value = false;
  error.value = "";
}
watch(
  () => props.disabled,
  (disabled) => {
    if (disabled) abort();
  },
);
onDeactivated(abort);
onBeforeUnmount(abort);

async function attach(event: Event) {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !input.files?.length) return;
  const files = Array.from(input.files);
  input.value = "";
  if (reading.value || props.disabled) return;
  const previous = attachments.value;
  const encodedSize =
    previous.reduce((bytes, file) => bytes + file.content.length, 0) +
    files.reduce((bytes, file) => bytes + 4 * Math.ceil(file.size / 3), 0);
  if (
    previous.length + files.length > 16 ||
    encodedSize > maxEncoded ||
    files.some((file) => file.size > maxFile || [...file.name].length > 255)
  ) {
    error.value = t("email.attachmentError");
    return;
  }
  error.value = "";
  reading.value = true;
  const selected: EmailAttachment[] = [];
  try {
    for (const file of files) {
      const content = await read(file);
      if (!reader || attachments.value !== previous || props.disabled) return;
      selected.push({
        name: file.name,
        content_type: file.type || "application/octet-stream",
        content,
      });
    }
    attachments.value = [...previous, ...selected];
  } catch {
    if (reader && attachments.value === previous)
      error.value = t("email.attachmentError");
  } finally {
    reader = undefined;
    reading.value = false;
  }
}
</script>

<template>
  <div class="grid gap-2">
    <UiField
      :label="$t('email.attachments')"
      :help="$t('email.attachmentHelp')"
      v-slot="field"
    >
      <input
        :id="field.id"
        type="file"
        multiple
        :disabled="disabled || reading"
        :aria-describedby="field.describedby"
        class="max-w-full text-sm text-muted"
        @change="attach"
      />
    </UiField>
    <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
    <div
      v-for="(attachment, index) in attachments"
      :key="index"
      class="flex items-center justify-between gap-2"
    >
      <span class="truncate text-sm">{{ attachment.name }}</span>
      <UiButton
        variant="ghost"
        size="sm"
        :disabled="disabled || reading"
        :aria-label="$t('email.removeAttachment', { name: attachment.name })"
        @click="
          attachments = attachments.filter((_, position) => position !== index)
        "
        >{{ $t("email.remove") }}</UiButton
      >
    </div>
  </div>
</template>
