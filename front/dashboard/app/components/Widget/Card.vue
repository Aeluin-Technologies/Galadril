<script setup>
defineProps({
  title: String,
  value: String,
  icon: [Object, Function],
  trend: String,
  size: {
    type: String,
    default: "md",
  },
});
</script>

<template>
  <div
    class="bg-surface border border-border rounded-xl flex items-center transition-all hover:ring-2 hover:ring-focus group"
    :class="{
      'p-2 gap-2': size === 'sm',
      'p-4 gap-4': size === 'md',
      'p-6 gap-6': size === 'lg',
    }"
  >
    <div
      class="bg-canvas rounded-lg text-primary-foreground group-hover:bg-primary-subtle transition-colors shrink-0"
      :class="{
        'p-2': size === 'sm',
        'p-3': size === 'md',
        'p-4': size === 'lg',
      }"
    >
      <component
        :is="icon"
        :class="{
          'w-4 h-4': size === 'sm',
          'w-6 h-6': size === 'md',
          'w-8 h-8': size === 'lg',
        }"
      />
    </div>

    <div class="min-w-0 flex-1">
      <p
        class="font-bold text-muted uppercase tracking-tighter truncate leading-tight"
        :class="{
          'text-[9px]': size === 'sm',
          'text-xs': size === 'md',
          'text-sm': size === 'lg',
        }"
      >
        {{ title }}
      </p>
      <div class="flex items-center gap-2">
        <h3
          class="font-black text-foreground tabular-nums"
          :class="{
            'text-sm': size === 'sm',
            'text-2xl': size === 'md',
            'text-4xl': size === 'lg',
          }"
        >
          {{ value }}
        </h3>
        <span
          v-if="trend && trend !== 'Stable'"
          :class="[
            trend.startsWith('+')
              ? 'text-success bg-success-subtle'
              : 'text-danger bg-danger-subtle',
            size === 'sm' ? 'text-[8px] px-1' : 'text-[10px] px-1.5 py-0.5',
          ]"
          class="rounded-full font-bold"
        >
          {{ trend }}{{ trend.includes("%") ? "" : "%" }}
        </span>
      </div>
    </div>
  </div>
</template>
