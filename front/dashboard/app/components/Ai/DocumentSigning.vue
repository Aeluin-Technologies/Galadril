<script setup lang="ts">
import {
  ArrowDownTrayIcon,
  ArrowLeftIcon,
  ArrowRightIcon,
  CheckCircleIcon,
  KeyIcon,
  PencilSquareIcon,
  XMarkIcon,
} from "@heroicons/vue/24/outline";
import { SpanStatusCode, trace } from "@opentelemetry/api";
import {
  pengoloMutation,
  pengoloOperation,
  pengoloRequest,
} from "~/utils/pengolo-api";
import {
  inspectSigningLayout,
  type SigningPlacement,
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
const { t, locale } = useI18n();
const { session } = useAccount();
const tracer = trace.getTracer("galadril.dashboard");
const vault = shallowRef<SigningVault | null>();
const original = shallowRef<Uint8Array<ArrayBuffer>>();
const fields = shallowRef<Awaited<ReturnType<typeof inspectSigningLayout>>>([]);
const placement = ref<SigningPlacement>();
const step = ref(1);
const signingForm = useId();
const signedUrl = ref("");
const passkeyUnavailable = ref(false);
const checkingPasskey = ref(false);
const marks = shallowRef<
  { id: string; label: string; includeCaption: boolean }[]
>([]);
const selectedField = ref("");
const selectedMark = ref("");
const password = ref("");
const confirmation = ref("");
const enrollPasskey = ref(false);

const busy = ref(false);
const error = ref("");

const supportsPasskey = typeof PublicKeyCredential !== "undefined";
const fieldOptions = computed(() =>
  fields.value.map(({ name }) => ({ label: name, value: name })),
);
const markOptions = computed(() =>
  marks.value.map(({ id, label }) => ({ label, value: id })),
);
const hasVisual = computed(() => !!selectedField.value || !!placement.value);
const placementReady = computed(
  () =>
    !!original.value &&
    vault.value !== undefined &&
    (!hasVisual.value || !!selectedMark.value),
);
const usePasskey = computed(
  () => !!vault.value?.passkey && supportsPasskey && !passkeyUnavailable.value,
);
const canSign = computed(() => placementReady.value && step.value === 2);
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
    const available = await inspectSigningLayout(bytes);
    const savedMarks = await pengoloRequest<
      { id: string; label: string; includeCaption: boolean }[]
    >(pengoloOperation("PengoloHandwrittenSignatures"), {}, signal);
    if (signal.aborted) return;
    original.value = bytes;
    fields.value = available;
    marks.value = savedMarks;
    selectedField.value = available[0]?.name ?? "";
    placement.value = available[0]?.placement;
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
  if (signedUrl.value) URL.revokeObjectURL(signedUrl.value);
  original.value = undefined;
  password.value = "";
  confirmation.value = "";
});

watch(selectedField, (name) => {
  placement.value = fields.value.find(
    (field) => field.name === name,
  )?.placement;
});

async function authenticate() {
  step.value = 2;
  error.value = "";
  if (!usePasskey.value) return;
  checkingPasskey.value = true;
  try {
    passkeyUnavailable.value =
      !(await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable());
  } catch {
    // A probe failure gives no evidence that a credential is absent.
  } finally {
    checkingPasskey.value = false;
  }
}

async function confirmSigning() {
  const passkey = usePasskey.value;
  const bytes = original.value;
  const actor = session.value;
  if (
    busy.value ||
    checkingPasskey.value ||
    signedUrl.value ||
    !actor ||
    !bytes ||
    !canSign.value ||
    vault.value === undefined ||
    (passkey ? !vault.value?.passkey : !canUsePassword.value)
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
    if (passkey && enrolled.passkey)
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
    const drawing = hasVisual.value
      ? await pengoloRequest<string | null>(
          pengoloOperation("PengoloHandwrittenSignatureTrace"),
          { id: mark },
          signal,
        )
      : null;
    if (hasVisual.value && !drawing)
      throw new Error("Signature drawing unavailable");
    if (!current()) return;
    const includeCaption =
      drawing && marks.value.find((item) => item.id === mark)?.includeCaption;
    const profile = includeCaption
      ? await pengoloRequest<{
          firstName: string;
          lastName: string;
          email: string;
        }>(pengoloOperation("PengoloDocumentProfile"), {}, signal)
      : undefined;
    if (!current()) return;
    const signerName = profile
      ? [profile.firstName, profile.lastName]
          .map((part) => part.trim())
          .filter(Boolean)
          .join(" ") ||
        profile.email.trim() ||
        (actor.displayName?.includes("@") ? actor.displayName.trim() : "")
      : "";
    const time = new Date();
    const lines = signerName
      ? [
          t("documentSigning.signedBy", { name: signerName }),
          t("documentSigning.signedOn", {
            date: new Intl.DateTimeFormat(locale.value, {
              dateStyle: "short",
              timeStyle: "short",
            }).format(time),
          }),
        ]
      : undefined;
    const signed = await signPdf(
      bytes,
      enrolled,
      key,
      drawing
        ? {
            field,
            placement: placement.value,
            trace: parseSigningTrace(drawing),
            caption: lines,
          }
        : null,
      time,
    );
    if (!current()) return;
    signedUrl.value = URL.createObjectURL(
      new Blob([new Uint8Array(signed)], { type: "application/pdf" }),
    );
    step.value = 3;
    span.setStatus({ code: SpanStatusCode.OK });
  } catch (failure) {
    if (
      current() &&
      passkey &&
      errorKey === "documentSigning.unlockFailed" &&
      failure instanceof DOMException &&
      ["NotSupportedError", "NotFoundError"].includes(failure.name)
    ) {
      passkeyUnavailable.value = true;
    }
    span.setStatus({ code: SpanStatusCode.ERROR });
    if (current()) error.value = t(errorKey);
  } finally {
    secret = "";
    prf?.fill(0);
    busy.value = false;
    span.end();
  }
}

function back() {
  step.value = 1;
  password.value = "";
  confirmation.value = "";
  error.value = "";
}

function downloadSigned() {
  if (!signedUrl.value) return;
  const link = document.createElement("a");
  link.href = signedUrl.value;
  link.download = props.name.replace(/\.pdf$/i, "") + "-signed.pdf";
  link.click();
}
</script>

<template>
  <UiDialog
    :open="true"
    :size="step === 1 ? 'wide' : 'default'"
    :title="$t('documentSigning.title')"
    :description="name"
    :dismissible="!busy || !original"
    @close="emit('close')"
  >
    <p v-if="busy && !original" role="status">
      {{ $t("documentSigning.loading") }}
    </p>
    <div
      v-if="step === 1 && original"
      class="grid gap-4 md:grid-cols-[14rem_minmax(0,1fr)]"
    >
      <div class="space-y-3">
        <h3 class="font-semibold">{{ $t("documentSigning.placement") }}</h3>
        <p class="text-sm text-muted">
          {{ $t("documentSigning.placementHelp") }}
        </p>
        <UiSelect
          v-if="fields.length > 1"
          v-model="selectedField"
          :label="$t('documentSigning.field')"
          :options="fieldOptions"
        />
        <UiSelect
          v-if="marks.length"
          v-model="selectedMark"
          :label="$t('documentSigning.mark')"
          :options="markOptions"
        />
        <p v-else class="text-sm text-muted">
          {{ $t("documentSigning.noMark") }}
        </p>
        <UiButton
          v-if="placement && !selectedField"
          variant="ghost"
          @click="placement = undefined"
        >
          <XMarkIcon aria-hidden="true" />{{
            $t("documentSigning.clearPlacement")
          }}
        </UiButton>
        <p v-if="!hasVisual" class="text-sm text-muted">
          {{ $t("documentSigning.invisible") }}
        </p>
      </div>
      <AiSigningPlacement
        v-model="placement"
        :bytes="original"
        :editable="marks.length > 0"
      />
    </div>
    <form
      v-if="step === 2"
      :id="signingForm"
      class="space-y-4"
      @submit.prevent="confirmSigning"
    >
      <p class="text-sm text-muted">{{ $t("documentSigning.consent") }}</p>
      <p v-if="usePasskey" class="text-sm text-muted">
        {{ $t("documentSigning.passkeyRequired") }}
      </p>
      <template v-else>
        <UiInput
          v-model="password"
          type="password"
          :label="$t('documentSigning.password')"
          :autocomplete="vault ? 'current-password' : 'new-password'"
          :disabled="busy || checkingPasskey"
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
      </template>
    </form>
    <p v-if="step === 3" role="status" class="flex items-center gap-2">
      <CheckCircleIcon class="size-5 text-success" aria-hidden="true" />
      {{ $t("documentSigning.complete") }}
    </p>
    <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
    <template #footer>
      <UiButton
        v-if="step === 2"
        variant="secondary"
        :disabled="busy"
        @click="back"
      >
        <ArrowLeftIcon aria-hidden="true" />{{ $t("documentSigning.back") }}
      </UiButton>
      <UiButton v-else variant="secondary" @click="emit('close')">
        {{ $t(step === 1 ? "actions.cancel" : "actions.close") }}
      </UiButton>
      <UiButton
        v-if="step === 1"
        :disabled="!placementReady"
        @click="authenticate"
      >
        {{ $t("documentSigning.continue")
        }}<ArrowRightIcon aria-hidden="true" />
      </UiButton>
      <UiButton
        v-else-if="step === 2"
        type="submit"
        :form="signingForm"
        :loading="busy"
        :disabled="checkingPasskey || (usePasskey ? !canSign : !canUsePassword)"
      >
        <KeyIcon v-if="usePasskey" aria-hidden="true" /><PencilSquareIcon
          v-else
          aria-hidden="true"
        />
        {{
          $t(
            usePasskey
              ? "documentSigning.signWithPasskey"
              : "documentSigning.sign",
          )
        }}
      </UiButton>
      <UiButton v-else @click="downloadSigned">
        <ArrowDownTrayIcon aria-hidden="true" />{{
          $t("documentSigning.downloadSigned")
        }}
      </UiButton>
    </template>
  </UiDialog>
</template>
