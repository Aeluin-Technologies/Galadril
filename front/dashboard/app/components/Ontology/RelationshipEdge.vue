<script setup lang="ts">
import { BaseEdge } from "@vue-flow/core";
import type { CSSProperties } from "vue";
defineOptions({ inheritAttrs: false });
const props = defineProps<{
  id: string;
  source: string;
  target: string;
  sourceX: number;
  sourceY: number;
  targetX: number;
  targetY: number;
  label?: string | object;
  data?: { offset?: number };
  style?: CSSProperties;
  editable?: boolean;
}>();
const emit = defineEmits<{ inspect: [] }>();
const geometry = computed(() => {
  const distance = Math.max(80, Math.abs(props.targetX - props.sourceX) / 3);
  const offset =
    props.source === props.target ? -140 : (props.data?.offset ?? 0);
  return {
    path: `M ${props.sourceX},${props.sourceY} C ${props.sourceX + distance},${props.sourceY + offset} ${props.targetX - distance},${props.targetY + offset} ${props.targetX},${props.targetY}`,
    x: (props.sourceX + props.targetX) / 2,
    y: (props.sourceY + props.targetY) / 2 + offset * 0.75,
  };
});
</script>
<template>
  <g
    :tabindex="editable ? 0 : undefined"
    :role="editable ? 'button' : undefined"
    :aria-label="typeof label === 'string' ? label : undefined"
    class="focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus"
    @keydown.enter.stop.prevent="emit('inspect')"
    @keydown.space.stop.prevent="emit('inspect')"
  >
    <BaseEdge
      :id="id"
      :path="geometry.path"
      :label="label"
      :label-x="geometry.x"
      :label-y="geometry.y"
      :style="style"
      :interaction-width="18"
      :label-bg-style="{ fill: 'rgb(var(--ui-surface))' }"
      :label-style="{ fill: 'rgb(var(--ui-foreground))' }"
    />
  </g>
</template>
