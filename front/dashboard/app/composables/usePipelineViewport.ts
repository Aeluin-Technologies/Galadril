import { useVueFlow } from "@vue-flow/core";

/** Fits measured nodes when the canvas changes, including KeepAlive reactivation. */
export function usePipelineViewport() {
  const flow = useVueFlow("pipeline-editor");
  const canvas = ref<HTMLDivElement | null>(null);
  const active = ref(true);
  onActivated(() => {
    active.value = true;
  });
  onDeactivated(() => {
    active.value = false;
  });
  watch(
    [canvas, active],
    ([element, enabled], _, cleanup) => {
      if (!element || !enabled) return;
      const observer = new ResizeObserver(() => {
        void flow.fitView({ padding: 0.2 });
      });
      observer.observe(element);
      cleanup(() => observer.disconnect());
    },
    { flush: "post" },
  );
  return { flow, canvas };
}
