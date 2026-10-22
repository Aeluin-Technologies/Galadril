<script setup lang="ts">
import { useCommandBarLogic } from "~/composables/useCommandBar";
import {
  MagnifyingGlassIcon,
  XMarkIcon,
  CheckCircleIcon,
  InformationCircleIcon,
  ArrowPathIcon,
} from "@heroicons/vue/24/outline";

const emit = defineEmits(["select"]);

const {
  isCommandOpen,
  inputRef,
  activeIndex,
  searchQuery,
  isLoading,
  hasMore,
  loadMore,
  searchFailed,
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
        class="fixed inset-0 bg-foreground/40 backdrop-blur-[2px] z-[100] flex justify-center pt-32 p-4"
      >
        <div
          @click.stop
          role="dialog"
          aria-modal="true"
          aria-label="Search Galadril"
          class="bg-surface border border-border w-full max-w-[620px] h-fit rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        >
          <div
            v-if="localAlertMessage"
            class="bg-danger-subtle border-b border-danger px-4 py-3 text-xs text-danger flex items-center justify-between"
          >
            <span>{{ localAlertMessage }}</span>
            <UiButton @click="localAlertMessage = null" variant="danger">
              X
            </UiButton>
          </div>

          <div
            class="flex items-center border-b border-border px-4 py-4 relative"
          >
            <MagnifyingGlassIcon class="w-6 h-6 text-muted mr-3" />
            <UiInput
              ref="inputRef"
              v-model="searchQuery"
              type="text"
              aria-label="Search Galadril"
              maxlength="512"
              :placeholder="$t('command_palette.placeholder')"
              class="w-full placeholder:text-muted"
            />
            <span
              v-if="isLoading"
              role="status"
              aria-label="Searching Galadril"
              class="absolute right-4 text-primary-foreground"
              ><ArrowPathIcon aria-hidden="true" class="h-4 w-4 animate-spin"
            /></span>
          </div>

          <div
            class="bg-primary-subtle/60 border-b border-border/70 px-4 py-2.5 flex items-start space-x-2 text-xs text-primary-foreground"
          >
            <InformationCircleIcon
              class="w-4 h-4 text-primary-foreground mt-0.5 shrink-0"
            />
            <div>
              <span class="font-semibold">{{
                $t("command_palette.advanced_search.title")
              }}</span>
              {{ $t("command_palette.advanced_search.description") }}
              <code
                class="bg-primary-subtle/80 px-1 py-0.5 rounded text-[11px] font-mono mx-0.5"
                >entity_id:E101</code
              >,
              <code
                class="bg-primary-subtle/80 px-1 py-0.5 rounded text-[11px] font-mono mx-0.5"
                >event:type</code
              >, or
              <code
                class="bg-primary-subtle/80 px-1 py-0.5 rounded text-[11px] font-mono mx-0.5"
                >modality:vision</code
              >.
            </div>
          </div>

          <div class="max-h-[380px] overflow-y-auto p-2">
            <p
              v-if="searchFailed"
              role="alert"
              class="p-3 text-sm text-danger"
            >
              Search could not be completed. Try again.
            </p>
            <div
              v-if="!searchQuery.trim() && commandHistory.length > 0"
              class="px-3 py-1.5 text-[10px] font-bold text-muted uppercase tracking-wider"
            >
              {{ $t("command_palette.sections.recent_executions") }}
            </div>

            <div
              v-if="visibleItems.length === 0 && !isLoading"
              class="px-3 py-8 text-center text-xs text-muted"
            >
              {{ $t("command_palette.empty_state") }}
            </div>

            <div v-else class="space-y-1">
              <template v-for="(item, index) in visibleItems" :key="index">
                <div
                  v-if="!searchQuery.trim() && index === commandHistory.length"
                  class="px-3 pt-3 pb-1.5 text-[10px] font-bold text-muted uppercase tracking-wider"
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
                  class="px-3 py-1 text-[10px] font-bold text-primary-foreground uppercase tracking-wider"
                >
                  {{ $t("command_palette.actions.suggested_action") }}
                </div>

                <div
                  @mouseenter="activeIndex = index"
                  @click="selectItem(item)"
                  :class="[
                    'flex items-center px-3 py-3 rounded-xl cursor-pointer transition-colors group relative',
                    activeIndex === index ? 'bg-primary-subtle' : 'hover:bg-primary-subtle',
                  ]"
                >
                  <component
                    :is="getHitIcon(item)"
                    :class="[
                      'w-5 h-5 mr-3 transition-colors shrink-0',
                      activeIndex === index
                        ? 'text-primary-foreground'
                        : 'text-muted group-hover:text-primary-foreground',
                    ]"
                  />

                  <div class="flex-1 min-w-0 pr-8">
                    <div
                      :class="[
                        'text-sm font-medium transition-colors truncate',
                        activeIndex === index
                          ? 'text-foreground'
                          : 'text-foreground group-hover:text-foreground',
                      ]"
                    >
                      {{ formatHitPayload(item).label }}
                    </div>
                    <div
                      :class="[
                        'text-xs transition-colors truncate',
                        activeIndex === index
                          ? 'text-primary-foreground/70'
                          : 'text-muted group-hover:text-primary-foreground/70',
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
                          ? 'text-primary-foreground bg-primary-subtle'
                          : 'text-muted bg-canvas group-hover:text-primary-foreground group-hover:bg-primary-subtle',
                      ]"
                    >
                      {{ formatHitPayload(item).type }}
                    </div>

                    <UiIconButton
                      v-if="
                        typeof item === 'object' &&
                        'isHistoryItem' in item &&
                        item.isHistoryItem
                      "
                      @click="handleRemoveHistory(index, $event)"
                      variant="danger"
                      :icon="XMarkIcon"
                      :label="$t('ui.actions.close')"
                    />
                  </div>
                </div>
              </template>
              <UiButton
                v-if="hasMore"
                variant="secondary"
                :disabled="isLoading"
                @click.stop="loadMore"
                >Load more results</UiButton
              >
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

  <UiDialog
    :open="isSuccessModalOpen"
    :title="$t('storage.upload.success_title')"
    @close="isSuccessModalOpen = false"
  >
    <div class="flex flex-col items-center text-center space-y-4 py-2">
      <div
        class="p-3 bg-success-subtle rounded-full text-success border border-success"
      >
        <CheckCircleIcon class="w-10 h-10" />
      </div>
      <div>
        <p class="text-sm text-muted">
          {{ $t("storage.upload.success_desc") }}
        </p>
      </div>
      <div
        class="w-full bg-canvas rounded-xl p-3 border border-border/60 font-mono text-xs text-muted break-all select-all"
      >
        {{ finalUploadedKey }}
      </div>
    </div>

    <template #footer>
      <UiButton @click="isSuccessModalOpen = false" variant="primary">
        {{ $t("storage.upload.success_btn") }}
      </UiButton>
    </template>
  </UiDialog>
</template>
