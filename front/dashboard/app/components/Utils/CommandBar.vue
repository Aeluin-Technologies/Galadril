<script setup lang="ts">
import { useCommandBarLogic } from "~/composables/useCommandBar";
import {
  MagnifyingGlassIcon,
  XMarkIcon,
  CheckCircleIcon,
  InformationCircleIcon,
} from "@heroicons/vue/24/outline";

const emit = defineEmits(["select"]);

const {
  isCommandOpen,
  inputRef,
  activeIndex,
  searchQuery,
  isLoading,
  visibleItems,
  commandHistory,
  localAlertMessage,
  isStagingModalOpen,
  isSuccessModalOpen,
  activeStagingData,
  finalUploadedKey,
  closeCommand,
  selectItem,
  handleRemoveHistory,
  onUploadSuccess,
  getHitIcon,
  formatHitPayload,
} = useCommandBarLogic(emit);
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
        v-if="isCommandOpen"
        @click="closeCommand"
        class="fixed inset-0 bg-slate-900/40 backdrop-blur-[2px] z-[100] flex justify-center pt-32 p-4"
      >
        <div
          @click.stop
          class="bg-white border border-slate-200 w-full max-w-[620px] h-fit rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        >
          <div
            v-if="localAlertMessage"
            class="bg-red-50 border-b border-red-100 px-4 py-3 text-xs text-red-800 flex items-center justify-between"
          >
            <span>{{ localAlertMessage }}</span>
            <button
              @click="localAlertMessage = null"
              class="text-red-400 hover:text-red-600 font-bold"
            >
              X
            </button>
          </div>

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
                  v-if="
                    searchQuery.trim() &&
                    typeof item === 'object' &&
                    'isStaticAction' in item &&
                    item.isStaticAction &&
                    index === 0
                  "
                  class="px-3 py-1 text-[10px] font-bold text-amber-600 uppercase tracking-wider"
                >
                  {{ $t("command_palette.actions.suggested_action") }}
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
                      v-if="
                        typeof item === 'object' &&
                        'isHistoryItem' in item &&
                        item.isHistoryItem
                      "
                      @click="handleRemoveHistory(index, $event)"
                      class="p-1 rounded-md text-slate-300 hover:text-red-500 hover:bg-red-50 transition-colors"
                    >
                      <XMarkIcon class="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              </template>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>

  <UploadStagingModal
    :is-open="isStagingModalOpen"
    :staging-data="activeStagingData"
    @close="isStagingModalOpen = false"
    @success="onUploadSuccess"
  />

  <UtilsModal
    :is-open="isSuccessModalOpen"
    :title="$t('storage.upload.success_title')"
    @close="isSuccessModalOpen = false"
  >
    <div class="flex flex-col items-center text-center space-y-4 py-2">
      <div
        class="p-3 bg-emerald-50 rounded-full text-emerald-600 border border-emerald-200"
      >
        <CheckCircleIcon class="w-10 h-10" />
      </div>
      <div>
        <p class="text-sm text-zinc-600">
          {{ $t("storage.upload.success_desc") }}
        </p>
      </div>
      <div
        class="w-full bg-stone-50 rounded-xl p-3 border border-zinc-200/60 font-mono text-xs text-zinc-600 break-all select-all"
      >
        {{ finalUploadedKey }}
      </div>
    </div>

    <template #footer>
      <button
        @click="isSuccessModalOpen = false"
        class="px-4 py-2 bg-amber-600 text-white hover:bg-amber-700 rounded-lg text-sm font-medium transition-colors shadow-sm focus:outline-none"
      >
        {{ $t("storage.upload.success_btn") }}
      </button>
    </template>
  </UtilsModal>
</template>
