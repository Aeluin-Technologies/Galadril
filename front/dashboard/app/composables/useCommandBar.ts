import { useCommandPaletteHistory } from "~/composables/useCommandPaletteHistory";
import { useCommandPaletteSearch } from "~/composables/useCommandPaletteSearch";
import { useS3Upload } from "~/composables/useS3Upload";
import { useOnboardingStore } from "~/stores/useOnboarding";
import { usePengoloSearch } from "~/stores/usePengoloSearch";
import {
  navigationEntries,
  searchNavigation,
  mergeNavigation,
} from "~/utils/navigation-search.mjs";
import {
  BoltIcon,
  DocumentTextIcon,
  PlusCircleIcon,
  CloudArrowUpIcon,
  ClockIcon,
  SparklesIcon,
  ChatBubbleLeftRightIcon,
} from "@heroicons/vue/24/outline";

export const useCommandBarLogic = (
  emit: (event: "select", ...args: any[]) => void,
) => {
  const { t, locale } = useI18n();

  const isCommandOpen = useState<boolean>("global-command-bar", () => false);
  const onboarding = useOnboardingStore();
  const { administrator } = useAccount();
  const conversations = usePengoloSearch();

  const inputRef = ref<HTMLInputElement | null>(null);
  const activeIndex = ref(0);

  const { commandHistory, pushQueryToHistory, removeHistoryItem } =
    useCommandPaletteHistory();
  const {
    searchQuery,
    isLoading,
    rawSearchResults,
    remoteNavigation,
    hasMore,
    loadMore,
    searchFailed,
  } = useCommandPaletteSearch();
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

  const navigationContext = computed(() => ({
    administrator: administrator.value,
    locale: locale.value,
  }));
  const command = (entry: ReturnType<typeof navigationEntries>[number]) => ({
    ...entry,
    type: entry.kind === "action" ? "Action" : "Page",
    icon:
      entry.kind === "action"
        ? PlusCircleIcon
        : entry.id === "documents" || entry.id === "library"
          ? DocumentTextIcon
          : BoltIcon,
    isStaticAction: true,
    isUploadAction: false,
    isHistoryItem: false,
  });
  const uploadAction = computed(() => ({
    label: t("command_palette.commands.upload.label"),
    description: t("command_palette.commands.upload.description"),
    type: t("command_palette.commands.upload.type"),
    icon: CloudArrowUpIcon,
    isStaticAction: true,
    isUploadAction: true,
    isHistoryItem: false,
  }));
  const defaultQuickActions = computed(() => [
    ...navigationEntries(navigationContext.value).map(command),
    uploadAction.value,
  ]);

  const matchedQuickActions = computed(() => {
    const query = searchQuery.value.trim().toLowerCase();
    if (!query) return [];
    const matches = searchNavigation(query, navigationContext.value).map(
      command,
    );
    const upload = uploadAction.value;
    if (
      upload.label.toLowerCase().includes(query) ||
      ["upload", "importer", "téléverser"].includes(query)
    )
      return [...matches, upload];
    return matches;
  });

  const visibleItems = computed(() => {
    const query = searchQuery.value.trim();
    if (!query) {
      return [...commandHistory.value, ...defaultQuickActions.value];
    }

    const staticMatches = mergeNavigation(
      matchedQuickActions.value.filter((entry) => "id" in entry),
      remoteNavigation.value,
    ).map(command);
    const searchResults = [...rawSearchResults.value];

    const upload = matchedQuickActions.value.some(
      (entry) => entry.isUploadAction,
    )
      ? [uploadAction.value]
      : [];
    return [
      ...staticMatches,
      ...upload,
      ...conversations.search(query),
      ...searchResults,
    ];
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
      case "chat":
      case "message":
        return ChatBubbleLeftRightIcon;
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

    if (item.kind === "chat" || item.kind === "message") {
      return {
        label: item.label,
        description:
          item.description ||
          (locale.value.startsWith("fr")
            ? "Conversation déjà chargée"
            : "Already loaded conversation"),
        type:
          item.kind === "chat"
            ? locale.value.startsWith("fr")
              ? "Conversation locale"
              : "Loaded chat"
            : locale.value.startsWith("fr")
              ? "Message local"
              : "Loaded message",
      };
    } else if (item.kind === "entity_state") {
      return {
        label: `${t("command_palette.item_types.entity")}: ${item.entityId}`,
        description: `${t("command_palette.item_types.state_properties")}: ${item.entityId}`,
        type: t("command_palette.item_types.entity"),
      };
    } else if (item.kind === "event") {
      return {
        label: `${t("command_palette.item_types.event")}: ${item.eventType}`,
        description: `ID: ${item.eventId} • ${t("command_palette.item_types.time")}: ${new Date(item.eventTimeMs).toLocaleTimeString()}`,
        type: t("command_palette.item_types.event"),
      };
    } else {
      return {
        label: `${t("command_palette.item_types.vector_match")} [${item.modality}]`,
        description: `${t("command_palette.item_types.distance")}: ${(item.score * 100).toFixed(1)}% • ${t("command_palette.item_types.entity")}: ${item.entityId}`,
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
      const stagingTicket = await requestStagingUpload();

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

    if (searchQuery.value.trim()) {
      pushQueryToHistory(searchQuery.value);
    }

    if ("href" in item && item.href) {
      await navigateTo(item.href);
      if (item.kind === "message" && typeof item.messageId === "string") {
        await nextTick();
        const target = document.getElementById(`p-message-${item.messageId}`);
        target?.scrollIntoView({ block: "center" });
        target?.focus({ preventScroll: true });
      }
    } else if ("entityId" in item && item.entityId) {
      await navigateTo(`/explorer/entity/${item.entityId}`);
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
  watch(administrator, () => {
    searchQuery.value = "";
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
  };
};
