import { ref, computed, watch, nextTick, onMounted, onUnmounted } from "vue";
import { useCommandPaletteHistory } from "~/composables/useCommandPaletteHistory";
import { useCommandPaletteSearch } from "~/composables/useCommandPaletteSearch";
import { useS3Upload } from "~/composables/useS3Upload";
import { useOnboardingStore } from "~/stores/useOnboarding";
import {
  BoltIcon,
  DocumentTextIcon,
  PlusCircleIcon,
  CloudArrowUpIcon,
  ClockIcon,
  SparklesIcon,
} from "@heroicons/vue/24/outline";

export const useCommandBarLogic = (
  emit: (event: "select", ...args: any[]) => void,
) => {
  const { t } = useI18n();

  const isCommandOpen = useState<boolean>("global-command-bar", () => false);
  const onboarding = useOnboardingStore();

  const inputRef = ref<HTMLInputElement | null>(null);
  const activeIndex = ref(0);

  const { commandHistory, pushQueryToHistory, removeHistoryItem } =
    useCommandPaletteHistory();
  const { searchQuery, isLoading, rawSearchResults } =
    useCommandPaletteSearch();
  const { requestStagingUpload, error: stagingError } = useS3Upload();

  const isStagingModalOpen = ref(false);
  const isSuccessModalOpen = ref(false);
  const finalUploadedKey = ref("");
  const activeStagingData = ref<{
    uploadUrl: string;
    stagingKey: string;
    originalName: string;
  } | null>(null);
  const localAlertMessage = ref<string | null>(null);

  const defaultQuickActions = computed(() => [
    {
      label: t("command_palette.commands.upload.label"),
      description: t("command_palette.commands.upload.description"),
      type: t("command_palette.commands.upload.type"),
      icon: CloudArrowUpIcon,
      isStaticAction: true,
      isUploadAction: true,
      isHistoryItem: false,
    },
    {
      label: t("command_palette.commands.approve_all.label"),
      description: t("command_palette.commands.approve_all.description"),
      type: t("command_palette.commands.approve_all.type"),
      icon: BoltIcon,
      isStaticAction: true,
      isUploadAction: false,
      isHistoryItem: false,
    },
    {
      label: t("command_palette.commands.generate_report.label"),
      description: t("command_palette.commands.generate_report.description"),
      type: t("command_palette.commands.generate_report.type"),
      icon: DocumentTextIcon,
      isStaticAction: true,
      isUploadAction: false,
      isHistoryItem: false,
    },
    {
      label: t("command_palette.commands.create_dashboard.label"),
      description: t("command_palette.commands.create_dashboard.description"),
      type: t("command_palette.commands.create_dashboard.type"),
      icon: PlusCircleIcon,
      href: "/builder",
      isStaticAction: true,
      isUploadAction: false,
      isHistoryItem: false,
    },
  ]);

  const matchedQuickActions = computed(() => {
    const query = searchQuery.value.trim().toLowerCase();
    if (!query) return [];
    return defaultQuickActions.value.filter(
      (action) =>
        action.label.toLowerCase().includes(query) ||
        action.description.toLowerCase().includes(query) ||
        action.type.toLowerCase().includes(query),
    );
  });

  const visibleItems = computed(() => {
    const query = searchQuery.value.trim();
    if (!query) {
      return [...commandHistory.value, ...defaultQuickActions.value];
    }

    const staticMatches = matchedQuickActions.value;
    let searchResults = [...rawSearchResults.value];

    if (staticMatches.length > 0 && searchResults.length > 0) {
      const truncateLength = Math.max(
        0,
        searchResults.length - staticMatches.length,
      );
      searchResults = searchResults.slice(0, truncateLength);
    }

    return [...staticMatches, ...searchResults];
  });

  const getHitIcon = (item: any) => {
    if (
      typeof item === "object" &&
      item !== null &&
      "isStaticAction" in item &&
      item.isStaticAction
    )
      return item.icon;
    if (
      typeof item === "object" &&
      item !== null &&
      "isHistoryItem" in item &&
      item.isHistoryItem
    )
      return ClockIcon;

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

  const formatHitPayload = (item: any) => {
    if (
      typeof item === "object" &&
      item !== null &&
      "isStaticAction" in item &&
      item.isStaticAction
    ) {
      return {
        label: item.label,
        description: item.description,
        type: item.type,
      };
    }
    if (
      typeof item === "object" &&
      item !== null &&
      "isHistoryItem" in item &&
      item.isHistoryItem
    ) {
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

  const closeCommand = () => {
    localAlertMessage.value = null;
    isCommandOpen.value = false;
  };

  const handleRemoveHistory = (index: number, event: Event) => {
    event.stopPropagation();
    removeHistoryItem(index);
    if (activeIndex.value >= visibleItems.value.length) {
      activeIndex.value = Math.max(0, visibleItems.value.length - 1);
    }
  };

  const selectItem = async (item: any) => {
    if (!item) return;

    if (
      typeof item === "object" &&
      "isHistoryItem" in item &&
      item.isHistoryItem
    ) {
      searchQuery.value = item.label;
      return;
    }

    if (
      typeof item === "object" &&
      "isUploadAction" in item &&
      item.isUploadAction
    ) {
      localAlertMessage.value = null;
      const placeholderName = `upload_${Date.now()}.png`;
      const stagingTicket = await requestStagingUpload(placeholderName);

      if (!stagingTicket) {
        localAlertMessage.value =
          stagingError.value ||
          t("command_palette.errors.insufficient_privileges");
        return;
      }

      activeStagingData.value = {
        ...stagingTicket,
        originalName: placeholderName,
      };
      isStagingModalOpen.value = true;
      closeCommand();
      return;
    }

    const isStatic =
      typeof item === "object" &&
      "isStaticAction" in item &&
      item.isStaticAction;
    if (searchQuery.value.trim() && !isStatic) {
      pushQueryToHistory(searchQuery.value);
    }

    if ("href" in item && item.href) {
      await navigateTo(item.href, { external: true });
    } else if ("entity_id" in item && item.entity_id) {
      await navigateTo(`/explorer/entity/${item.entity_id}`);
    } else {
      emit("select", item);
    }
    closeCommand();
  };

  const onUploadSuccess = (destKey: string) => {
    finalUploadedKey.value = destKey;
    isSuccessModalOpen.value = true;
    if (!onboarding.hasIngestedData) {
      onboarding.setStepCompleted("ingest");
    }
  };

  const handleKeyDown = (e: KeyboardEvent) => {
    if (!isCommandOpen.value) return;

    if (e.key === "Escape") {
      e.preventDefault();
      closeCommand();
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

  onMounted(() => {
    window.addEventListener("keydown", handleKeyDown);
  });
  onUnmounted(() => {
    window.removeEventListener("keydown", handleKeyDown);
  });

  watch(searchQuery, () => {
    activeIndex.value = 0;
  });

  watch(isCommandOpen, async (newVal) => {
    if (newVal) {
      activeIndex.value = 0;
      localAlertMessage.value = null;
      await nextTick();
      setTimeout(() => inputRef.value?.focus(), 50);
    } else {
      searchQuery.value = "";
    }
  });

  return {
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
  };
};
