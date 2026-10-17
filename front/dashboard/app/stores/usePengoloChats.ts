import { defineStore } from "pinia";
import { createRequestCache, mergeMessages } from "~/utils/pengolo";
import { usePengoloWorkspace } from "~/stores/usePengoloWorkspace";
import {
  pengoloMutation,
  pengoloRequest,
  pengoloOperation,
  chatRecord,
  type ChatResponse,
  type GenerationReplay,
  type ChatRecord,
} from "~/utils/pengolo-api";

export const usePengoloChats = defineStore("pengoloChats", () => {
  const items = ref<ChatRecord[]>([]);
  const active = ref<ChatRecord | null>(null);
  const loading = ref(false);
  const loadingOlder = ref(false);
  const cursors = ref<Record<string, string | null>>({});
  const after = computed(() => cursors.value[""] ?? null);
  const cache = createRequestCache<{
    items: ChatRecord[];
    after: string | null;
  }>();
  let identityEpoch = 0;
  async function load(more = false, projectId: string | null = null) {
    if (!more && !projectId) return usePengoloWorkspace().load();
    const epoch = identityEpoch;
    loading.value = true;
    try {
      const scope = projectId ?? "";
      const cursor = more ? (cursors.value[scope] ?? null) : null;
      const page = await cache.get(`chats:${scope}:${cursor ?? ""}`, () =>
        pengoloRequest<{ items: ChatRecord[]; after: string | null }>(
          pengoloOperation("Chats"),
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
    if (active.value?.id === id && active.value.data.messages)
      return active.value;
    const epoch = activate ? ++openEpoch : openEpoch;
    const record = chatRecord(
      await pengoloRequest<ChatResponse>(pengoloOperation("Chat"), {
        id,
        temporary,
      }),
    );
    if (activate && epoch === openEpoch) active.value = record;
    return record;
  }
  function upsert(chat: ChatRecord, temporary = false, activate = false) {
    const previous =
      active.value?.id === chat.id
        ? active.value
        : items.value.find((item) => item.id === chat.id);
    if (previous)
      chat = {
        ...chat,
        data: {
          ...previous.data,
          ...chat.data,
          messages: chat.data.messages ?? previous.data.messages,
          messagesBefore:
            chat.data.messagesBefore === undefined
              ? previous.data.messagesBefore
              : chat.data.messagesBefore,
        },
      };
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
  function hydrate(page: { items: ChatRecord[]; after: string | null }) {
    items.value = page.items;
    cursors.value[""] = page.after;
  }
  async function older(temporary = false) {
    const chat = active.value;
    const before = chat?.data.messagesBefore;
    if (!chat || !before || loadingOlder.value) return;
    loadingOlder.value = true;
    const epoch = identityEpoch;
    try {
      const page = await pengoloRequest<ChatResponse>(
        pengoloOperation("OlderMessages"),
        { id: chat.id, temporary, before },
      );
      if (
        epoch !== identityEpoch ||
        active.value?.id !== chat.id ||
        !page.data.messages
      )
        return;
      active.value.data.messages = mergeMessages(
        active.value.data.messages ?? [],
        page.data.messages.items,
        true,
      );
      active.value.data.messagesBefore = page.data.messages.after;
    } finally {
      if (epoch === identityEpoch) loadingOlder.value = false;
    }
  }
  function finish(chatId: string, page: GenerationReplay, temporary: boolean) {
    const chat =
      active.value?.id === chatId
        ? active.value
        : items.value.find((item) => item.id === chatId);
    if (!chat) return;
    upsert(
      {
        ...chat,
        revision: page.chatRevision ?? chat.revision,
        updatedAt: page.chatUpdatedAt ?? chat.updatedAt,
        data: {
          ...chat.data,
          activeGeneration:
            chat.data.activeGeneration === page.generationId
              ? null
              : chat.data.activeGeneration,
          messages: page.message
            ? mergeMessages(chat.data.messages ?? [], [page.message])
            : chat.data.messages,
        },
      },
      temporary,
    );
  }
  async function update(chat: ChatRecord, changes: Record<string, unknown>) {
    const epoch = identityEpoch;
    const updated = await pengoloMutation<ChatRecord>("updatePengoloChat", {
      id: chat.id,
      revision: chat.revision,
      ...changes,
    });
    if (epoch !== identityEpoch) throw new Error("Workspace identity changed");
    upsert(updated, changes.temporary === true);
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
    loadingOlder.value = false;
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
    loadingOlder,
    older,
    hydrate,
    finish,
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
