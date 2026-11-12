<script setup lang="ts">
import { SpanStatusCode, trace } from "@opentelemetry/api";
import {
  pengoloMutation,
  pengoloOperation,
  pengoloRequest,
} from "~/utils/pengolo-api";
import {
  inspectSigningFields,
  parseSigningTrace,
  signPdf,
} from "~/utils/pdf-signing";
import { createSigningVault, unlockSigningKey } from "~/utils/signing-vault";
import type { SigningVault } from "~/utils/signing-vault";
import {
  enrollSigningPasskey,
  unlockSigningPasskey,
} from "~/utils/signing-passkey";

const props = defineProps<{
  source: Blob;
  name: string;
  fileId: string;
  temporarySession?: string;
}>();
const emit = defineEmits<{ close: [] }>();
const { t } = useI18n();
const { session } = useAccount();
const tracer = trace.getTracer("galadril.dashboard");
const vault = shallowRef<SigningVault | null>();
const original = shallowRef<Uint8Array<ArrayBuffer>>();
const fields = shallowRef<string[]>([]);
const marks = shallowRef<{ id: string; label: string }[]>([]);
const selectedField = ref("");
const selectedMark = ref("");
const password = ref("");
const confirmation = ref("");
const enrollPasskey = ref(false);
const consent = ref(false);
const busy = ref(false);
const error = ref("");
const complete = ref(false);
const supportsPasskey = typeof PublicKeyCredential !== "undefined";
const fieldOptions = computed(() =>
  fields.value.map((name) => ({ label: name, value: name })),
);
const markOptions = computed(() =>
  marks.value.map(({ id, label }) => ({ label, value: id })),
);
const canSign = computed(
  () =>
    !!original.value &&
    consent.value &&
    (!fields.value.length || (!!selectedField.value && !!selectedMark.value)),
);
const canUsePassword = computed(
  () =>
    canSign.value &&
    !!password.value &&
    (vault.value !== null ||
      ([...password.value].length >= 15 &&
        password.value === confirmation.value)),
);
const controller = new AbortController();
const { signal } = controller;

onMounted(async () => {
  busy.value = true;
  try {
    const [buffer, stored] = await Promise.all([
      props.source.arrayBuffer(),
      pengoloRequest<string | null>(
        pengoloOperation("PengoloSigningVault"),
        {},
        signal,
      ),
    ]);
    const bytes = new Uint8Array(buffer);
    const available = await inspectSigningFields(bytes);
    const savedMarks = available.length
      ? await pengoloRequest<{ id: string; label: string }[]>(
          pengoloOperation("PengoloHandwrittenSignatures"),
          {},
          signal,
        )
      : [];
    if (signal.aborted) return;
    original.value = bytes;
    fields.value = available;
    marks.value = savedMarks;
    selectedField.value = available[0] ?? "";
    selectedMark.value = savedMarks[0]?.id ?? "";
    vault.value = stored === null ? null : JSON.parse(stored);
  } catch {
    if (!signal.aborted) error.value = t("documentSigning.unavailable");
  } finally {
    busy.value = false;
  }
});
onBeforeUnmount(() => {
  controller.abort();
  original.value = undefined;
  password.value = "";
  confirmation.value = "";
});

async function confirmSigning(usePasskey: boolean) {
  const bytes = original.value;
  const actor = session.value;
  if (
    busy.value ||
    complete.value ||
    !actor ||
    !bytes ||
    !canSign.value ||
    vault.value === undefined ||
    (usePasskey ? !vault.value?.passkey : !canUsePassword.value)
  )
    return;
  const scope = { tenant: actor.tenant, subject: actor.subject };
  const current = () =>
    !signal.aborted &&
    session.value?.tenant === scope.tenant &&
    session.value?.subject === scope.subject;
  let secret = password.value;
  let prf: Uint8Array<ArrayBuffer> | undefined;
  password.value = "";
  confirmation.value = "";
  busy.value = true;
  error.value = "";
  let errorKey = "documentSigning.failed";
  const span = tracer.startSpan("document.sign");
  try {
    if (!vault.value) {
      errorKey = "documentSigning.setupFailed";
      const enrollment = enrollPasskey.value
        ? await enrollSigningPasskey()
        : undefined;
      prf = enrollment?.prf;
      const created = await createSigningVault(scope, secret, enrollment);
      prf?.fill(0);
      prf = undefined;
      if (!current()) return;
      const saved = await pengoloRequest<boolean>(
        pengoloOperation("CreatePengoloSigningVault"),
        { envelope: JSON.stringify(created) },
        signal,
      );
      if (!current()) return;
      if (!saved) {
        const existing = await pengoloRequest<string | null>(
          pengoloOperation("PengoloSigningVault"),
          {},
          signal,
        );
        if (current() && existing) vault.value = JSON.parse(existing);
        throw new Error("Signing vault already exists");
      }
      vault.value = created;
    }
    const enrolled = vault.value;
    errorKey = "documentSigning.unlockFailed";
    if (usePasskey && enrolled.passkey)
      prf = await unlockSigningPasskey(enrolled.passkey);
    const key = await unlockSigningKey(
      scope,
      enrolled,
      prf ? { prf } : { password: secret },
    );
    secret = "";
    prf?.fill(0);
    if (!current()) return;
    errorKey = "documentSigning.failed";
    // Recheck access while signing the frozen bytes already shown in the preview.
    await pengoloMutation(
      "pengoloDownload",
      { id: props.fileId, temporarySession: props.temporarySession },
      signal,
    );
    const field = selectedField.value;
    const mark = selectedMark.value;
    const drawing = fields.value.length
      ? await pengoloRequest<string | null>(
          pengoloOperation("PengoloHandwrittenSignatureTrace"),
          { id: mark },
          signal,
        )
      : null;
    if (fields.value.length && !drawing)
      throw new Error("Signature drawing unavailable");
    if (!current()) return;
    const signed = await signPdf(
      bytes,
      enrolled,
      key,
      drawing ? { field, trace: parseSigningTrace(drawing) } : null,
    );
    if (!current()) return;
    const url = URL.createObjectURL(
      new Blob([new Uint8Array(signed)], { type: "application/pdf" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = props.name.replace(/\.pdf$/i, "") + "-signed.pdf";
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
    complete.value = true;
    span.setStatus({ code: SpanStatusCode.OK });
  } catch {
    span.setStatus({ code: SpanStatusCode.ERROR });
    if (current()) error.value = t(errorKey);
  } finally {
    secret = "";
    prf?.fill(0);
    busy.value = false;
    span.end();
  }
}
</script>

<template>
  <section
    class="mb-4 space-y-3 rounded-panel border border-border p-4"
    :aria-label="$t('documentSigning.title')"
  >
    <p v-if="busy && !original" role="status">
      {{ $t("documentSigning.loading") }}
    </p>
    <p v-if="complete" role="status">{{ $t("documentSigning.complete") }}</p>
    <form
      v-else-if="vault !== undefined"
      class="space-y-3"
      @submit.prevent="confirmSigning(false)"
    >
      <p class="text-sm text-muted">
        {{ $t("documentSigning.certificateNotice") }}
      </p>
      <template v-if="fields.length">
        <UiSelect
          v-model="selectedField"
          :label="$t('documentSigning.field')"
          :options="fieldOptions"
          :disabled="busy"
        />
        <UiSelect
          v-if="marks.length"
          v-model="selectedMark"
          :label="$t('documentSigning.mark')"
          :options="markOptions"
          :disabled="busy"
        />
        <p v-else>{{ $t("documentSigning.noMark") }}</p>
      </template>
      <p v-else>{{ $t("documentSigning.invisible") }}</p>
      <UiCheckbox
        v-model="consent"
        :label="$t('documentSigning.consent')"
        :disabled="busy"
      />
      <UiInput
        v-model="password"
        type="password"
        :label="$t('documentSigning.password')"
        :autocomplete="vault ? 'current-password' : 'new-password'"
        :disabled="busy"
      />
      <template v-if="!vault">
        <UiInput
          v-model="confirmation"
          type="password"
          :label="$t('documentSigning.confirmPassword')"
          autocomplete="new-password"
          :help="$t('documentSigning.passwordHelp')"
          :disabled="busy"
        />
        <UiCheckbox
          v-if="supportsPasskey"
          v-model="enrollPasskey"
          :label="$t('documentSigning.enablePasskey')"
          :disabled="busy"
        />
      </template>
      <div class="flex flex-wrap gap-2">
        <UiButton
          type="button"
          variant="secondary"
          :disabled="busy"
          @click="emit('close')"
          >{{ $t("actions.cancel") }}</UiButton
        >
        <UiButton type="submit" :loading="busy" :disabled="!canUsePassword">{{
          $t("documentSigning.sign")
        }}</UiButton>
        <UiButton
          v-if="vault?.passkey && supportsPasskey"
          type="button"
          variant="secondary"
          :disabled="busy || !canSign"
          @click="confirmSigning(true)"
          >{{ $t("documentSigning.signWithPasskey") }}</UiButton
        >
      </div>
    </form>
    <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
  </section>
</template>
