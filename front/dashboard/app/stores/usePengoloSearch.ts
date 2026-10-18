import { defineStore } from "pinia";
import { metrics } from "@opentelemetry/api";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { usePengoloWorkspace } from "~/stores/usePengoloWorkspace";
import {
  createConversationIndex,
  type LocalConversationHit,
} from "~/utils/pengolo-search";

export const usePengoloSearch = defineStore("pengoloSearch", () => {
  const chats = usePengoloChats();
  const ui = usePengoloUi();
  const workspace = usePengoloWorkspace();
  const revision = ref(0);
  let indexedRevision = -1;
  let index: ReturnType<typeof createConversationIndex> | null = null;
  const meter = metrics.getMeter("galadril.dashboard.search");
  const indexDuration = meter.createHistogram(
    "galadril.search.local.index_duration",
    { unit: "ms" },
  );
  const queryDuration = meter.createHistogram(
    "galadril.search.local.query_duration",
    { unit: "ms" },
  );
  const projectionBytes = meter.createHistogram(
    "galadril.search.local.projection_bytes",
    { unit: "By" },
  );

  function clear() {
    index = null;
    indexedRevision = -1;
    revision.value++;
  }

  watch(
    () => [
      workspace.ready,
      workspace.identity?.version,
      chats.items,
      chats.active,
      chats.active?.data.messages,
      ui.temporary,
    ],
    clear,
    { flush: "sync" },
  );

  /** Reuse the index across keystrokes without persisting another conversation cache. */
  function search(query: string): LocalConversationHit[] {
    const current = revision.value;
    if (!workspace.ready || !query.trim()) return [];
    if (!index || indexedRevision !== current) {
      const began = performance.now();
      index = createConversationIndex(chats.items, chats.active, {
        temporary: ui.temporary,
      });
      indexedRevision = current;
      indexDuration.record(performance.now() - began);
      projectionBytes.record(index.stats.textBytes);
    }
    const began = performance.now();
    const hits = index.search(query);
    queryDuration.record(performance.now() - began);
    return hits;
  }

  return { search, clear };
});
