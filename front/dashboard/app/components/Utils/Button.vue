<script setup lang="ts">
const props = defineProps({
  variant: {
    type: String,
    default: "primary",
  },
  tooltip: {
    type: [String, Boolean],
    default: undefined,
  },
});
const attrs = useAttrs();
const title = computed(() => {
  if (props.tooltip === false) return undefined;
  const label = props.tooltip ?? attrs.title ?? attrs["aria-label"];
  return typeof label === "string" ? label : undefined;
});
</script>

<template>
  <button
    :class="[
      'inline-flex items-center justify-center px-3 py-1.5 rounded-lg text-xs font-medium transition-colors shadow-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-500 disabled:opacity-50 disabled:cursor-not-allowed',
      variant === 'primary' ? 'bg-amber-600 text-white hover:bg-amber-700' : '',
      variant === 'secondary'
        ? 'bg-white border border-zinc-200 text-zinc-700 hover:bg-stone-50'
        : '',
      variant === 'danger'
        ? 'bg-white border border-zinc-200 text-red-600 hover:bg-red-50 hover:border-red-200'
        : '',
      variant === 'ghost'
        ? 'bg-transparent shadow-none text-zinc-500 hover:text-zinc-800 hover:bg-stone-100'
        : '',
    ]"
    v-bind="$attrs"
    :title="title"
  >
    <slot />
  </button>
</template>
