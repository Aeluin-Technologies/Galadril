import { defineStore } from "pinia";
import { createRequestCache } from "~/utils/pengolo";
import {
  pengoloMutation,
  pengoloRequest,
  type ChatRecord,
} from "~/utils/pengolo-api";

export const usePengoloChats = defineStore("pengoloChats", () => {
  const items = ref<ChatRecord[]>([]);
  const active = ref<ChatRecord | null>(null);
  const loading = ref(false);
  const cursors = ref<Record<string, string | null>>({});
  const after = computed(() => cursors.value[""] ?? null);
  const cache = createRequestCache<{
    items: ChatRecord[];
    after: string | null;
  }>();
  let identityEpoch = 0;
  async function load(more = false, projectId: string | null = null) {
    const epoch = identityEpoch;
    loading.value = true;
    try {
      const scope = projectId ?? "";
      const cursor = more ? (cursors.value[scope] ?? null) : null;
      const page = await cache.get(`chats:${scope}:${cursor ?? ""}`, () =>
        pengoloRequest<{ items: ChatRecord[]; after: string | null }>(
          "query Chats($after: String, $project: String) { pengoloChats(after: $after, projectId: $project) }",
          { after: cursor, project: projectId },
        ),
      );
      if (epoch !== identityEpoch) return;
      const others = items.value.filter((chat) => chat.projectId !== projectId);
      const previous = items.value.filter(
        (chat) => chat.projectId === projectId,
      );
      items.value = [
        ...others,
        ...(more
          ? [
              ...previous,
              ...page.items.filter(
                (item) => !previous.some((old) => old.id === item.id),
              ),
            ]
          : page.items),
      ];
      cursors.value[scope] = page.after;
    } finally {
      if (epoch === identityEpoch) loading.value = false;
    }
  }
  let openEpoch = 0;
  async function open(id: string, temporary = false, activate = true) {
    const epoch = activate ? ++openEpoch : openEpoch;
    const record = await pengoloRequest<ChatRecord>(
      "query Chat($id: String!, $temporary: Boolean!) { pengoloChat(id: $id, temporary: $temporary) }",
      { id, temporary },
    );
    if (activate && epoch === openEpoch) active.value = record;
    return record;
  }
  function upsert(chat: ChatRecord, temporary = false, activate = false) {
    if (activate || active.value?.id === chat.id) active.value = chat;
    if (!temporary) {
      items.value = [
        chat,
        ...items.value.filter((item) => item.id !== chat.id),
      ].sort(
        (a, b) =>
          Number(b.data.pinned) - Number(a.data.pinned) ||
          b.updatedAt - a.updatedAt,
      );
      cache.clear();
    }
  }
  async function update(chat: ChatRecord, changes: Record<string, unknown>) {
    const epoch = identityEpoch;
    const updated = await pengoloMutation<ChatRecord>("updatePengoloChat", {
      id: chat.id,
      revision: chat.revision,
      ...changes,
    });
    if (epoch !== identityEpoch) throw new Error("Workspace identity changed");
    upsert(updated);
    return updated;
  }
  async function remove(chat: ChatRecord) {
    const epoch = identityEpoch;
    await pengoloMutation("deletePengoloChat", {
      id: chat.id,
      revision: chat.revision,
    });
    if (epoch !== identityEpoch) throw new Error("Workspace identity changed");
    items.value = items.value.filter((item) => item.id !== chat.id);
    if (active.value?.id === chat.id) active.value = null;
    cache.clear();
  }
  function clear() {
    identityEpoch++;
    loading.value = false;
    openEpoch++;
    cache.clear();
    items.value = [];
    active.value = null;
    cursors.value = {};
  }
  return {
    items,
    active,
    loading,
    after,
    cursors,
    load,
    open,
    upsert,
    update,
    remove,
    clear,
  };
});
