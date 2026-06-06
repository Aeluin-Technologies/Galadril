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
    class="bg-white border border-zinc-200 rounded-xl flex items-center transition-all hover:ring-2 hover:ring-amber-100 group"
    :class="{
      'p-2 gap-2': size === 'sm',
      'p-4 gap-4': size === 'md',
      'p-6 gap-6': size === 'lg',
    }"
  >
    <div
      class="bg-stone-50 rounded-lg text-amber-600 group-hover:bg-amber-50 transition-colors shrink-0"
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
        class="font-bold text-zinc-500 uppercase tracking-tighter truncate leading-tight"
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
          class="font-black text-zinc-900 tabular-nums"
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
              ? 'text-emerald-600 bg-emerald-50'
              : 'text-rose-600 bg-rose-50',
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
