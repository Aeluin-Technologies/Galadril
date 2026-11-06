<script setup lang="ts">
import {
  ArrowDownTrayIcon,
  ArrowTopRightOnSquareIcon,
  PaperClipIcon,
} from "@heroicons/vue/24/outline";
import {
  buildRedactionRules,
  redactLocalDocument,
  type RedactedDocument,
} from "~/utils/pengolo-redaction";

const open = defineModel<boolean>("open", { default: false });
const { t } = useI18n();
const input = ref<HTMLInputElement | null>(null);
const file = shallowRef<File | null>(null);
const names = ref("");
const pattern = ref("");
const emails = ref(true),
  phones = ref(true),
  addresses = ref(false);
const busy = ref(false);
const error = ref("");
const result = shallowRef<RedactedDocument | null>(null);
const url = ref("");
let controller: AbortController | null = null;
const options = computed(() => ({
  names: names.value,
  pattern: pattern.value,
  emails: emails.value,
  phones: phones.value,
  addresses: addresses.value,
}));
const ready = computed(() => {
  if (!file.value || busy.value) return false;
  try {
    buildRedactionRules(options.value);
    return true;
  } catch {
    return false;
  }
});
function clearResult() {
  if (url.value) URL.revokeObjectURL(url.value);
  url.value = "";
  result.value = null;
}
function reset() {
  controller?.abort();
  controller = null;
  busy.value = false;
  file.value = null;
  names.value = "";
  pattern.value = "";
  error.value = "";
  if (input.value) input.value.value = "";
  clearResult();
}
function choose(files: readonly File[]) {
  if (busy.value) return;
  if (files.length !== 1) {
    error.value = t("pengolo.redaction.chooseOne");
    return;
  }
  file.value = files[0] ?? null;
  error.value = "";
  clearResult();
}
function changed(event: Event) {
  if (event.target instanceof HTMLInputElement)
    choose(Array.from(event.target.files ?? []));
}
async function run() {
  if (!file.value || busy.value) return;
  const pending = new AbortController();
  controller = pending;
  busy.value = true;
  error.value = "";
  clearResult();
  try {
    const output = await redactLocalDocument(
      file.value,
      buildRedactionRules(options.value),
      pending.signal,
    );
    if (!open.value || pending.signal.aborted) return;
    result.value = output;
    url.value = URL.createObjectURL(output.blob);
  } catch {
    if (!pending.signal.aborted) error.value = t("pengolo.redaction.failed");
  } finally {
    if (controller === pending) {
      busy.value = false;
      controller = null;
    }
  }
}
watch(options, clearResult);
watch(open, (value) => {
  if (!value) reset();
});
onDeactivated(reset);
onBeforeUnmount(reset);
</script>

<template>
  <UiDialog
    v-model:open="open"
    :title="t('pengolo.redaction.title')"
    :description="t('pengolo.redaction.private')"
    :close-label="t('actions.close')"
  >
    <div class="space-y-4">
      <UiFileDrop
        :label="t('pengolo.redaction.drop')"
        :disabled="busy"
        @files="choose"
      >
        <UiField
          :label="t('pengolo.redaction.localDocument')"
          :help="t('pengolo.redaction.fileHelp')"
          v-slot="{ id }"
        >
          <input
            :id="id"
            ref="input"
            type="file"
            accept=".pdf,.txt"
            class="sr-only"
            :disabled="busy"
            @change="changed"
          />
          <UiButton variant="secondary" :disabled="busy" @click="input?.click()"
            ><PaperClipIcon aria-hidden="true" />{{
              file?.name ?? t("pengolo.redaction.choose")
            }}</UiButton
          >
        </UiField>
      </UiFileDrop>
      <div class="flex flex-wrap gap-4">
        <UiCheckbox
          v-model="emails"
          :label="t('pengolo.redaction.emails')"
          :disabled="busy"
        />
        <UiCheckbox
          v-model="phones"
          :label="t('pengolo.redaction.phones')"
          :disabled="busy"
        />
        <UiCheckbox
          v-model="addresses"
          :label="t('pengolo.redaction.addresses')"
          :disabled="busy"
        />
      </div>
      <UiField
        :label="t('pengolo.redaction.names')"
        :help="t('pengolo.redaction.namesHelp')"
        v-slot="{ id }"
      >
        <UiTextarea
          :id="id"
          v-model="names"
          rows="2"
          maxlength="6144"
          :disabled="busy"
        />
      </UiField>
      <details class="rounded-control border border-border p-3 text-ui">
        <summary class="cursor-pointer text-muted">
          {{ t("pengolo.redaction.custom") }}
        </summary>
        <UiField
          :label="t('pengolo.redaction.pattern')"
          :help="t('pengolo.redaction.patternHelp')"
          v-slot="{ id }"
        >
          <UiTextarea
            :id="id"
            v-model="pattern"
            rows="2"
            maxlength="512"
            :disabled="busy"
          />
        </UiField>
      </details>
      <p v-if="error" role="alert" class="text-ui text-danger">{{ error }}</p>
      <div
        v-if="result"
        role="status"
        class="space-y-2 rounded-control bg-surface-muted p-3 text-ui"
      >
        <strong>{{
          t("pengolo.redaction.completed", {
            matches: result.matches,
            pages: result.pages,
          })
        }}</strong>
        <p class="text-muted">{{ t("pengolo.redaction.review") }}</p>
        <p v-if="result.annotationsRemoved" class="text-muted">
          {{ t("pengolo.redaction.annotations") }}
        </p>
        <div class="flex flex-wrap gap-4">
          <a
            :href="url"
            :download="
              result.blob.type === 'application/pdf'
                ? 'redacted-document.pdf'
                : 'redacted-document.txt'
            "
            class="inline-flex items-center gap-2 text-info hover:underline"
            ><ArrowDownTrayIcon class="size-4" aria-hidden="true" />{{
              t("pengolo.redaction.download")
            }}</a
          >
          <a
            :href="url"
            target="_blank"
            rel="noopener noreferrer"
            class="inline-flex items-center gap-2 text-info hover:underline"
            ><ArrowTopRightOnSquareIcon class="size-4" aria-hidden="true" />{{
              t("pengolo.redaction.preview")
            }}</a
          >
        </div>
      </div>
    </div>
    <template #footer
      ><UiButton
        :action="run"
        :disabled="!ready"
        :loading-label="t('pengolo.redaction.working')"
        >{{ t("pengolo.redaction.create") }}</UiButton
      ></template
    >
  </UiDialog>
</template>
