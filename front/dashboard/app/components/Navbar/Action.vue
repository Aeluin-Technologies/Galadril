<script setup>
import {
  XMarkIcon,
  CheckIcon,
  PencilSquareIcon,
  NoSymbolIcon,
  ExclamationTriangleIcon,
  ArrowTopRightOnSquareIcon,
} from "@heroicons/vue/24/outline";

const props = defineProps({
  selectedItem: {
    type: Object,
    default: () => null,
  },
});

const emit = defineEmits(["close", "approve", "modify", "reject"]);
</script>

<template>
  <aside
    v-if="selectedItem"
    class="w-80 bg-stone-50 border-l border-zinc-200 flex flex-col h-full shadow-lg relative z-30"
  >
    <div class="p-5 border-b border-zinc-200 bg-white">
      <div class="flex items-center justify-between mb-3">
        <span
          class="text-[10px] uppercase tracking-widest text-zinc-400 font-bold"
        >
          {{ selectedItem.id }}
        </span>
        <button
          @click="emit('close')"
          class="text-zinc-400 hover:text-amber-700 hover:bg-amber-100 p-1 rounded-full transition"
        >
          <XMarkIcon class="w-5 h-5" />
        </button>
      </div>
      <h2 class="text-xl font-bold text-zinc-950 leading-tight">
        {{ selectedItem.target }}
      </h2>
      <p class="text-xs text-zinc-500 mt-1">
        {{ $t("details_panel.common.status_label") }}
        <span
          :class="[
            'font-medium',
            selectedItem.severity === 'critical'
              ? 'text-red-600'
              : 'text-amber-700',
          ]"
        >
          {{ selectedItem.status }}
        </span>
      </p>
    </div>

    <div class="p-5 space-y-4">
      <p
        class="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider"
      >
        {{ $t("details_panel.common.operational_actions") }}
      </p>

      <button
        @click="emit('approve', selectedItem.id)"
        class="w-full flex items-center justify-between p-3.5 bg-amber-100 hover:bg-amber-200 text-amber-700 rounded-xl transition font-semibold group"
      >
        <span>{{ $t("details_panel.actions.approve") }}</span>
        <CheckIcon
          class="w-5 h-5 group-hover:translate-x-0.5 transition-transform"
        />
      </button>

      <button
        @click="emit('modify', selectedItem.id)"
        class="w-full flex items-center justify-between p-3.5 bg-white hover:bg-stone-100 text-zinc-800 rounded-xl transition border border-zinc-200 shadow-sm hover:border-zinc-300"
      >
        <span>{{ $t("details_panel.actions.modify") }}</span>
        <PencilSquareIcon class="w-5 h-5 text-zinc-400" />
      </button>

      <button
        @click="emit('reject', selectedItem.id)"
        class="w-full flex items-center justify-between p-3.5 hover:bg-red-50 text-red-600 rounded-xl transition border border-transparent hover:border-red-100 group"
      >
        <span class="font-medium">{{
          $t("details_panel.actions.reject")
        }}</span>
        <NoSymbolIcon
          class="w-5 h-5 text-red-400 opacity-70 group-hover:opacity-100"
        />
      </button>
    </div>

    <div class="mt-auto p-5 border-t border-zinc-200 bg-white">
      <div
        :class="[
          'flex items-center space-x-2 text-xs mb-2.5 px-3 py-1.5 rounded-lg border',
          selectedItem.severity === 'critical'
            ? 'text-red-800 bg-red-50 border-red-100'
            : 'text-amber-800 bg-amber-50 border-amber-100',
        ]"
      >
        <ExclamationTriangleIcon
          :class="[
            'w-4 h-4',
            selectedItem.severity === 'critical'
              ? 'text-red-600'
              : 'text-amber-600',
          ]"
        />
        <span class="font-medium">{{ selectedItem.issue }}</span>
      </div>
      <button
        class="text-xs text-amber-700 hover:text-amber-900 hover:underline flex items-center group"
      >
        {{ $t("details_panel.common.view_logic") }}
        <ArrowTopRightOnSquareIcon
          class="w-3 h-3 ml-1.5 opacity-70 group-hover:opacity-100"
        />
      </button>
    </div>
  </aside>

  <aside
    v-else
    class="w-80 bg-stone-50 border-l border-zinc-200 flex items-center justify-center h-full"
  >
    <p class="text-zinc-400 text-xs italic">
      {{ $t("details_panel.empty_state") }}
    </p>
  </aside>
</template>
