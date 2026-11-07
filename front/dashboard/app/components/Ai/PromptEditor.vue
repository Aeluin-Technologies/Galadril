<script setup lang="ts">
const props = defineProps<{
  modelValue: string;
  label: string;
  placeholder: string;
  disabled?: boolean;
  hasPrefix?: boolean;
}>();
const emit = defineEmits<{
  "update:modelValue": [value: string];
  submit: [];
  removePrefix: [];
}>();
const editor = ref<HTMLElement | null>(null);
const viewport = ref<HTMLElement | null>(null);
function value() {
  const element = editor.value;
  if (!element) return "";
  return element.textContent ? element.innerText.replace(/\r\n/g, "\n") : "";
}
function sync() {
  if (!editor.value || value() === props.modelValue) return;
  editor.value.textContent = props.modelValue;
  if (!props.modelValue && viewport.value) viewport.value.scrollTop = 0;
}
watch(() => props.modelValue, sync);
onMounted(sync);
onActivated(sync);
function update() {
  const text = value();
  let end = Math.min(text.length, 32768);
  if (end < text.length && /[\uD800-\uDBFF]/.test(text.charAt(end - 1))) end--;
  const next = text.slice(0, end);
  if (next !== text && editor.value) editor.value.textContent = next;
  emit("update:modelValue", next);
}
function focus() {
  if (!props.disabled) editor.value?.focus();
}
function keydown(event: KeyboardEvent) {
  if (props.disabled || event.isComposing) return;
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    emit("submit");
    return;
  }
  if (!props.hasPrefix || !["Backspace", "Delete"].includes(event.key)) return;
  const selection = window.getSelection();
  const element = editor.value;
  if (
    !element ||
    !selection?.isCollapsed ||
    !selection.anchorNode ||
    !element.contains(selection.anchorNode)
  )
    return;
  const before = document.createRange();
  before.selectNodeContents(element);
  before.setEnd(selection.anchorNode, selection.anchorOffset);
  if (!before.toString()) {
    event.preventDefault();
    emit("removePrefix");
  }
}
function paste(event: ClipboardEvent) {
  event.preventDefault();
  if (props.disabled || !editor.value) return;
  const text = (event.clipboardData?.getData("text/plain") ?? "").slice(
    0,
    32768,
  );
  const selection = window.getSelection();
  if (!selection?.rangeCount) return;
  const range = selection.getRangeAt(0);
  if (!editor.value.contains(range.commonAncestorContainer)) return;
  range.deleteContents();
  const node = document.createTextNode(text);
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
  update();
}
</script>

<template>
  <div
    ref="viewport"
    class="p-composer-entry cursor-text text-sm leading-6"
    @click.self="focus"
  >
    <span v-if="hasPrefix" class="p-composer-prefix" contenteditable="false"
      ><slot name="prefix" /></span
    ><span
      ref="editor"
      role="textbox"
      aria-multiline="true"
      :aria-label="label"
      :aria-disabled="disabled || undefined"
      :contenteditable="disabled ? 'false' : 'plaintext-only'"
      :tabindex="disabled ? -1 : 0"
      :data-placeholder="placeholder"
      class="p-composer-editor whitespace-pre-wrap break-words outline-none empty:before:pointer-events-none empty:before:text-muted empty:before:content-[attr(data-placeholder)]"
      @input="update"
      @keydown="keydown"
      @paste="paste"
    />
  </div>
</template>
