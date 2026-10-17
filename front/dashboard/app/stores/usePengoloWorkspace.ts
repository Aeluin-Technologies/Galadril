import { defineStore } from "pinia";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloModels, type PengoloModel } from "~/stores/usePengoloModels";
import { usePengoloUi } from "~/stores/usePengoloUi";
import {
  pengoloData,
  pengoloOperation,
  chatRecord,
  type ChatRecord,
  type ChatResponse,
  type ProjectRecord,
  type WorkspaceIdentity,
} from "~/utils/pengolo-api";

export const usePengoloWorkspace = defineStore("pengoloWorkspace", () => {
  const identity = ref<WorkspaceIdentity | null>(null);
  const ready = ref(false);
  const unavailableChat = ref<string | null>(null);
  let pending: Promise<void> | null = null;
  let epoch = 0;
  async function load(): Promise<void> {
    if (ready.value) return;
    if (pending) return pending;
    const generation = epoch;
    const id = useRouter().currentRoute.value.path.match(
      /^\/pengolo\/([a-f0-9]{32})$/,
    )?.[1];
    const request = pengoloData<{
      pengoloIdentity: WorkspaceIdentity;
      pengoloModels: { defaultModel: string | null; models: PengoloModel[] };
      pengoloProjects: ProjectRecord[];
      pengoloChats: { items: ChatRecord[]; after: string | null };
      activeChat?: ChatResponse | null;
    }>(pengoloOperation("Workspace"), { id: id ?? "", withChat: !!id }, true)
      .then((data) => {
        if (generation !== epoch) throw new Error("Workspace identity changed");
        identity.value = data.pengoloIdentity;
        usePengoloModels().hydrate(data.pengoloModels);
        usePengoloProjects().items = data.pengoloProjects;
        usePengoloChats().hydrate(data.pengoloChats);
        if (data.activeChat)
          usePengoloChats().active = chatRecord(data.activeChat);
        unavailableChat.value = id && !data.activeChat ? id : null;
        usePengoloUi().webAvailable = data.pengoloIdentity.webSearchAvailable;
        useState("account-administrator", () => false).value =
          data.pengoloIdentity.administrator;
        ready.value = true;
        window.dispatchEvent(new Event("pengolo-identity-verified"));
      })
      .finally(() => {
        if (pending === request) pending = null;
      });
    pending = request;
    return request;
  }
  function clear() {
    epoch++;
    pending = null;
    ready.value = false;
    identity.value = null;
    unavailableChat.value = null;
  }
  return { identity, ready, unavailableChat, load, clear };
});
