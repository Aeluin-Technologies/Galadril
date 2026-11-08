<script setup lang="ts">
import {
  nextTick,
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  onMounted,
  ref,
  useId,
  watch,
} from "vue";
import { XMarkIcon } from "@heroicons/vue/20/solid";
import IconButton from "./IconButton.vue";
defineOptions({ inheritAttrs: false });

const props = withDefaults(
  defineProps<{
    open: boolean;
    title: string;
    description?: string;
    closeLabel?: string;
    dismissible?: boolean;
    size?: "default" | "wide";
    placement?: "center" | "right";
    modal?: boolean;
  }>(),
  {
    closeLabel: "Close dialog",
    dismissible: true,
    size: "default",
    placement: "center",
    modal: true,
  },
);
const emit = defineEmits<{ "update:open": [value: boolean]; close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const id = useId();
let opener: HTMLElement | null = null;
let active = false;
async function synchronize() {
  await nextTick();
  if (props.open && active && dialog.value && !dialog.value.open) {
    opener =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    if (props.modal) dialog.value.showModal();
    else {
      dialog.value.show();
      dialog.value.focus();
      document.addEventListener("keydown", nonModalEscape);
    }
  } else if (!props.open && dialog.value?.open) {
    dialog.value.close();
    document.removeEventListener("keydown", nonModalEscape);
    opener?.focus();
  }
}
function nonModalEscape(event: KeyboardEvent) {
  if (
    active &&
    props.open &&
    !props.modal &&
    event.key === "Escape" &&
    !event.defaultPrevented
  ) {
    event.preventDefault();
    close();
  }
}
function close() {
  if (props.dismissible) {
    emit("update:open", false);
    emit("close");
  }
}
function backdrop(event: MouseEvent) {
  if (event.target !== dialog.value || !dialog.value) return;
  const rect = dialog.value.getBoundingClientRect();
  if (
    event.clientX < rect.left ||
    event.clientX > rect.right ||
    event.clientY < rect.top ||
    event.clientY > rect.bottom
  )
    close();
}
/** Keeps the tab cycle in the modal instead of allowing focus into browser chrome. */
function keydown(event: KeyboardEvent) {
  if (!props.modal) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
    return;
  }
  if (event.defaultPrevented || event.key !== "Tab" || !dialog.value) return;
  const elements = Array.from(
    dialog.value.querySelectorAll<HTMLElement>(
      "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
    ),
  ).filter(
    (element) =>
      element.getClientRects().length > 0 && !element.closest("[inert]"),
  );
  const first = elements.at(0);
  const last = elements.at(-1);
  if (!first || !last) {
    event.preventDefault();
    dialog.value.focus();
    return;
  }
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
watch(() => props.open, synchronize, { immediate: true });
onMounted(() => {
  active = true;
  void synchronize();
});
onActivated(() => {
  active = true;
  void synchronize();
});
onDeactivated(() => {
  active = false;
  document.removeEventListener("keydown", nonModalEscape);
  dialog.value?.close();
});
onBeforeUnmount(() => {
  document.removeEventListener("keydown", nonModalEscape);
  dialog.value?.close();
  if (opener?.isConnected) opener.focus();
});
</script>

<template>
  <Teleport to="body">
    <dialog
      v-bind="$attrs"
      ref="dialog"
      tabindex="-1"
      data-ui="dialog"
      :class="
        placement === 'right'
          ? 'fixed inset-y-0 right-0 z-[70] m-0 ml-auto h-dvh max-h-dvh w-full max-w-xl rounded-none'
          : [
              size === 'wide' ? 'max-w-6xl' : 'max-w-lg',
              'm-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] rounded-dialog',
            ]
      "
      :aria-labelledby="`${id}-title`"
      :aria-describedby="description ? `${id}-description` : undefined"
      class="overflow-y-auto border border-border bg-surface p-0 text-foreground shadow-dialog"
      @cancel.prevent="close"
      @click="backdrop"
      @keydown="keydown"
    >
      <header
        class="flex items-start justify-between gap-4 border-b border-border px-6 py-5"
      >
        <div class="min-w-0 flex-1">
          <h2
            :id="`${id}-title`"
            class="text-ui-section font-semibold [overflow-wrap:anywhere]"
          >
            {{ title }}
          </h2>
          <p
            v-if="description"
            :id="`${id}-description`"
            class="mt-2 text-ui text-muted"
          >
            {{ description }}
          </p>
        </div>
        <IconButton
          v-if="dismissible"
          :icon="XMarkIcon"
          :label="closeLabel"
          @click="close"
        />
      </header>
      <div class="p-6"><slot /></div>
      <footer
        v-if="$slots.footer"
        class="flex flex-wrap justify-end gap-3 border-t border-border bg-canvas px-6 py-4"
      >
        <slot name="footer" />
      </footer>
    </dialog>
  </Teleport>
</template>
