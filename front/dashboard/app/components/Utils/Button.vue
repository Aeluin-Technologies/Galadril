<script setup>
defineProps({
  variant: {
    type: String,
    default: "primary",
  },
});
</script>

<template>
  <button
    :class="[
      'inline-flex items-center justify-center px-4 py-2 rounded-lg text-sm font-medium transition-colors shadow-sm focus:outline-none',
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
  >
    <slot />
  </button>
</template>
