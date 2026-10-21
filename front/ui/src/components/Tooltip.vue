<script setup lang="ts">
import {
  onBeforeUnmount,
  onDeactivated,
  ref,
  useId,
  watch,
  type CSSProperties,
} from "vue";
import {
  arrow,
  autoUpdate,
  computePosition,
  flip,
  offset,
  shift,
} from "@floating-ui/dom";
import { recordOverlayFailure } from "../action-metrics";

const props = defineProps<{
  target: HTMLElement | null;
  text: string;
  delay?: number;
}>();
const id = useId();
const bubble = ref<HTMLElement | null>(null);
const pointer = ref<HTMLElement | null>(null);
const pointerPosition = ref<CSSProperties>({});
const ready = ref(false);
const side = ref<"top" | "bottom">("top");
const position = ref({ left: "0px", top: "0px" });
let timer: ReturnType<typeof setTimeout> | undefined;
let cleanup: (() => void) | undefined;
let dismissed = false;
let stopPositioning: (() => void) | undefined;
let generation = 0;

function clearTimer() {
  if (timer !== undefined) clearTimeout(timer);
  timer = undefined;
}
function hide() {
  clearTimer();
  generation++;
  ready.value = false;
  stopPositioning?.();
  stopPositioning = undefined;
  if (bubble.value?.matches(":popover-open")) bubble.value.hidePopover();
  document.removeEventListener("keydown", escape, true);
}
async function updatePosition() {
  const current = generation;
  if (!props.target || !bubble.value || !pointer.value) return;
  try {
    const result = await computePosition(props.target, bubble.value, {
      strategy: "fixed",
      placement: "top",
      middleware: [
        offset(8),
        flip({ padding: 8, fallbackPlacements: ["bottom"] }),
        shift({ padding: 8 }),
        arrow({ element: pointer.value, padding: 8 }),
      ],
    });
    if (current !== generation) return;
    side.value = result.placement.startsWith("top") ? "top" : "bottom";
    position.value = { left: `${result.x}px`, top: `${result.y}px` };
    // Keep the pointer on its trigger when viewport shifting clamps the arrow near a corner.
    const arrowData = result.middlewareData.arrow;
    pointerPosition.value = {
      left: `${(arrowData?.x ?? 0) + (arrowData?.centerOffset ?? 0)}px`,
      [side.value === "top" ? "bottom" : "top"]: "-4px",
    };
    ready.value = true;
  } catch {
    if (current === generation) {
      recordOverlayFailure();
      hide();
    }
  }
}
function show() {
  clearTimer();
  if (
    dismissed ||
    !props.text ||
    !props.target?.isConnected ||
    !bubble.value?.isConnected
  )
    return;
  if (stopPositioning) return;
  bubble.value.showPopover();
  stopPositioning = autoUpdate(
    props.target,
    bubble.value,
    () => void updatePosition(),
  );
  document.addEventListener("keydown", escape, true);
}
function focus() {
  dismissed = false;
  show();
}
function enter() {
  dismissed = false;
  clearTimer();
  timer = setTimeout(() => {
    show();
  }, props.delay ?? 300);
}
function leave() {
  clearTimer();
  timer = setTimeout(hide, 120);
}
function escape(event: KeyboardEvent) {
  if (event.key !== "Escape" || !bubble.value?.matches(":popover-open")) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  dismissed = true;
  hide();
}
watch(
  () => props.target,
  (target) => {
    cleanup?.();
    if (!target) return;
    const previous =
      target.getAttribute("aria-describedby")?.split(/\s+/).filter(Boolean) ??
      [];
    target.setAttribute("aria-describedby", [...previous, id].join(" "));
    target.addEventListener("pointerenter", enter);
    target.addEventListener("pointerleave", leave);
    target.addEventListener("focus", focus);
    target.addEventListener("blur", hide);
    target.addEventListener("click", hide);
    cleanup = () => {
      hide();
      target.removeEventListener("pointerenter", enter);
      target.removeEventListener("pointerleave", leave);
      target.removeEventListener("focus", focus);
      target.removeEventListener("blur", hide);
      target.removeEventListener("click", hide);
      document.removeEventListener("keydown", escape, true);
      const remaining = target
        .getAttribute("aria-describedby")
        ?.split(/\s+/)
        .filter((value) => value !== id)
        .join(" ");
      if (remaining) target.setAttribute("aria-describedby", remaining);
      else target.removeAttribute("aria-describedby");
    };
  },
  { flush: "post", immediate: true },
);
onDeactivated(hide);
onBeforeUnmount(() => cleanup?.());
</script>

<template>
  <span
    :id="id"
    ref="bubble"
    data-ui="tooltip"
    :data-side="side"
    role="tooltip"
    popover="manual"
    class="fixed inset-auto m-0 overflow-visible rounded-control border-0 bg-foreground text-surface shadow-popover"
    :style="{
      ...position,
      margin: 0,
      visibility: ready ? 'visible' : 'hidden',
    }"
    @pointerenter="clearTimer"
    @pointerleave="leave"
    >{{ text
    }}<span
      ref="pointer"
      data-ui="tooltip-arrow"
      aria-hidden="true"
      class="absolute size-2 rotate-45 bg-inherit"
      :style="pointerPosition"
  /></span>
</template>
