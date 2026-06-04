<script setup class="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from "vue";
import { useI18n } from "vue-i18n";
import { useCommandPaletteHistory } from "~/composables/useCommandPaletteHistory";
import { useCommandPaletteSearch } from "~/composables/useCommandPaletteSearch";
import {
  MagnifyingGlassIcon,
  BoltIcon,
  DocumentTextIcon,
  SparklesIcon,
  InformationCircleIcon,
  PlusCircleIcon,
  XMarkIcon,
  ClockIcon,
} from "@heroicons/vue/24/outline";

const props = defineProps({
  isOpen: Boolean,
});
const emit = defineEmits(["close", "select"]);

const { t } = useI18n();
const inputRef = (ref < HTMLInputElement) | (null > null);
const activeIndex = ref(0);

// Composable abstractions
const { commandHistory, pushQueryToHistory, removeHistoryItem } =
  useCommandPaletteHistory();
const { searchQuery, isLoading, rawSearchResults } = useCommandPaletteSearch();

onMounted(() => {
  window.addEventListener("keydown", handleKeyDown);
});

onUnmounted(() => {
  window.removeEventListener("keydown", handleKeyDown);
});

// Static Quick Actions computed with translation bindings
const defaultQuickActions = computed(() => [
  {
    label: t("command_palette.commands.approve_all.label"),
    description: t("command_palette.commands.approve_all.description"),
    type: t("command_palette.commands.approve_all.type"),
    icon: BoltIcon,
    isStaticAction: true,
  },
  {
    label: t("command_palette.commands.generate_report.label"),
    description: t("command_palette.commands.generate_report.description"),
    type: t("command_palette.commands.generate_report.type"),
    icon: DocumentTextIcon,
    isStaticAction: true,
  },
  {
    label: t("command_palette.commands.create_dashboard.label"),
    description: t("command_palette.commands.create_dashboard.description"),
    type: t("command_palette.commands.create_dashboard.type"),
    icon: PlusCircleIcon,
    href: "/builder",
    isStaticAction: true,
  },
]);

const visibleItems = computed(() => {
  if (!searchQuery.value.trim()) {
    return [...commandHistory.value, ...defaultQuickActions.value];
  }
  return rawSearchResults.value;
});

const getHitIcon = (item) => {
  if (item.isStaticAction) return item.icon;
  if (item.isHistoryItem) return ClockIcon;

  switch (item.kind) {
    case "entity_state":
      return BoltIcon;
    case "event":
      return DocumentTextIcon;
    case "embedding":
      return SparklesIcon;
    default:
      return BoltIcon;
  }
};

const formatHitPayload = (item) => {
  if (item.isStaticAction) {
    return {
      label: item.label,
      description: item.description,
      type: item.type,
    };
  }
  if (item.isHistoryItem) {
    return {
      label: item.label,
      description: t(item.description),
      type: t("command_palette.item_types.recent"),
    };
  }

  if (item.kind === "entity_state") {
    return {
      label: `${t("command_palette.item_types.entity")}: ${item.entity_id}`,
      description: `${t("command_palette.item_types.state_properties")}: ${JSON.stringify(item.payload).substring(0, 50)}...`,
      type: t("command_palette.item_types.entity"),
    };
  } else if (item.kind === "event") {
    return {
      label: `${t("command_palette.item_types.event")}: ${item.event_type}`,
      description: `ID: ${item.event_id} • ${t("command_palette.item_types.time")}: ${new Date(item.event_time_ms).toLocaleTimeString()}`,
      type: t("command_palette.item_types.event"),
    };
  } else {
    return {
      label: `${t("command_palette.item_types.vector_match")} [${item.modality}]`,
      description: `${t("command_palette.item_types.distance")}: ${(item.score * 100).toFixed(1)}% • ${t("command_palette.item_types.entity")}: ${item.entity_id}`,
      type: t("command_palette.item_types.embedding"),
    };
  }
};

const handleRemoveHistory = (index, event) => {
  event.stopPropagation();
  removeHistoryItem(index);
  if (activeIndex.value >= visibleItems.value.length) {
    activeIndex.value = Math.max(0, visibleItems.value.length - 1);
  }
};

const close = () => {
  emit("close");
};

const selectItem = async (item) => {
  if (!item) return;

  if (item.isHistoryItem) {
    searchQuery.value = item.label;
    return;
  }

  if (searchQuery.value.trim() && !item.isStaticAction) {
    pushQueryToHistory(searchQuery.value);
  }

  if (item.href) {
    await navigateTo(item.href, { external: true });
  } else if (item.entity_id) {
    await navigateTo(`/explorer/entity/${item.entity_id}`);
  } else {
    emit("select", item);
  }
  close();
};

watch(searchQuery, () => {
  activeIndex.value = 0;
});

watch(
  () => props.isOpen,
  async (newVal) => {
    if (newVal) {
      activeIndex.value = 0;
      await nextTick();
      setTimeout(() => inputRef.value?.focus(), 50);
    } else {
      searchQuery.value = "";
    }
  },
);

const handleKeyDown = (e) => {
  if (!props.isOpen) return;

  if (e.key === "Escape") {
    e.preventDefault();
    close();
  } else if (e.key === "ArrowDown") {
    e.preventDefault();
    activeIndex.value =
      (activeIndex.value + 1) % Math.max(1, visibleItems.value.length);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    activeIndex.value =
      (activeIndex.value - 1 + visibleItems.value.length) %
      Math.max(1, visibleItems.value.length);
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (visibleItems.value[activeIndex.value]) {
      selectItem(visibleItems.value[activeIndex.value]);
    }
  }
};
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition-opacity duration-200 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition-opacity duration-150 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="isOpen"
        @click="close"
        class="fixed inset-0 bg-slate-900/40 backdrop-blur-[2px] z-[100] flex justify-center pt-32 p-4"
      >
        <div
          @click.stop
          class="bg-white border border-slate-200 w-full max-w-[620px] h-fit rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        >
          <!-- Search input layout line -->
          <div
            class="flex items-center border-b border-slate-100 px-4 py-4 relative"
          >
            <MagnifyingGlassIcon class="w-6 h-6 text-slate-400 mr-3" />
            <input
              ref="inputRef"
              v-model="searchQuery"
              type="text"
              :placeholder="$t('command_palette.placeholder')"
              class="w-full bg-transparent border-none focus:ring-0 text-md text-slate-900 outline-none placeholder:text-slate-400"
            />
            <div
              v-if="isLoading"
              class="absolute right-4 animate-spin rounded-full h-4 w-4 border-2 border-amber-500 border-t-transparent"
            />
          </div>

          <!-- Documentation / Syntax Guide Notification Banner -->
          <div
            class="bg-amber-50/60 border-b border-amber-100/70 px-4 py-2.5 flex items-start space-x-2 text-xs text-amber-900"
          >
            <InformationCircleIcon
              class="w-4 h-4 text-amber-600 mt-0.5 shrink-0"
            />
            <div>
              <span class="font-semibold">{{
                $t("command_palette.advanced_search.title")
              }}</span>
              {{ $t("command_palette.advanced_search.description") }}
              <code
                class="bg-amber-100/80 px-1 py-0.5 rounded text-[11px] font-mono mx-0.5"
                >entity_id:E101</code
              >,
              <code
                class="bg-amber-100/80 px-1 py-0.5 rounded text-[11px] font-mono mx-0.5"
                >event:type</code
              >, or
              <code
                class="bg-amber-100/80 px-1 py-0.5 rounded text-[11px] font-mono mx-0.5"
                >modality:vision</code
              >.
            </div>
          </div>

          <!-- Scrollable Results Ledger list -->
          <div class="max-h-[380px] overflow-y-auto p-2">
            <div
              v-if="!searchQuery.trim() && commandHistory.length > 0"
              class="px-3 py-1.5 text-[10px] font-bold text-slate-400 uppercase tracking-wider"
            >
              {{ $t("command_palette.sections.recent_executions") }}
            </div>

            <div
              v-if="visibleItems.length === 0 && !isLoading"
              class="px-3 py-8 text-center text-xs text-slate-400"
            >
              {{ $t("command_palette.empty_state") }}
            </div>

            <div v-else class="space-y-1">
              <template v-for="(item, index) in visibleItems" :key="index">
                <div
                  v-if="!searchQuery.trim() && index === commandHistory.length"
                  class="px-3 pt-3 pb-1.5 text-[10px] font-bold text-slate-400 uppercase tracking-wider"
                >
                  {{ $t("command_palette.sections.quick_actions") }}
                </div>

                <div
                  @mouseenter="activeIndex = index"
                  @click="selectItem(item)"
                  :class="[
                    'flex items-center px-3 py-3 rounded-xl cursor-pointer transition-colors group relative',
                    activeIndex === index ? 'bg-amber-50' : 'hover:bg-amber-50',
                  ]"
                >
                  <component
                    :is="getHitIcon(item)"
                    :class="[
                      'w-5 h-5 mr-3 transition-colors shrink-0',
                      activeIndex === index
                        ? 'text-amber-600'
                        : 'text-slate-400 group-hover:text-amber-600',
                    ]"
                  />

                  <div class="flex-1 min-w-0 pr-8">
                    <div
                      :class="[
                        'text-sm font-medium transition-colors truncate',
                        activeIndex === index
                          ? 'text-slate-900'
                          : 'text-slate-700 group-hover:text-slate-900',
                      ]"
                    >
                      {{ formatHitPayload(item).label }}
                    </div>
                    <div
                      :class="[
                        'text-xs transition-colors truncate',
                        activeIndex === index
                          ? 'text-amber-700/70'
                          : 'text-slate-400 group-hover:text-amber-700/70',
                      ]"
                    >
                      {{ formatHitPayload(item).description }}
                    </div>
                  </div>

                  <div class="flex items-center space-x-2 shrink-0 ml-2">
                    <div
                      :class="[
                        'text-[10px] font-medium px-2 py-1 rounded-md uppercase transition-colors',
                        activeIndex === index
                          ? 'text-amber-500 bg-amber-100'
                          : 'text-slate-400 bg-slate-50 group-hover:text-amber-500 group-hover:bg-amber-100',
                      ]"
                    >
                      {{ formatHitPayload(item).type }}
                    </div>

                    <button
                      v-if="item.isHistoryItem"
                      @click="handleRemoveHistory(index, $event)"
                      class="p-1 rounded-md text-slate-300 hover:text-red-500 hover:bg-red-50 transition-colors"
                      :title="$t('command_palette.tooltips.remove_trace')"
                    >
                      <XMarkIcon class="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              </template>
            </div>
          </div>

          <!-- Bottom Operational Action Bar Guidelines -->
          <div
            class="bg-slate-50 px-4 py-3 border-t border-slate-100 flex items-center justify-between text-[11px] text-slate-400"
          >
            <div class="flex space-x-4">
              <span>
                <kbd
                  class="bg-white border border-slate-200 px-1 rounded shadow-sm text-slate-500"
                  >↑↓</kbd
                >
                {{ $t("command_palette.footer.navigate") }}
              </span>
              <span>
                <kbd
                  class="bg-white border border-slate-200 px-1 rounded shadow-sm text-slate-500"
                  >enter</kbd
                >
                {{ $t("command_palette.footer.select") }}
              </span>
            </div>
            <span>
              {{ $t("command_palette.footer.press") }}
              <kbd
                class="bg-white border border-slate-200 px-1 rounded shadow-sm text-slate-500"
                >esc</kbd
              >
              {{ $t("command_palette.footer.close") }}
            </span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
