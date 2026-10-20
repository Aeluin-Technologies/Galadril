import { usePengoloAgents } from "~/stores/usePengoloAgents";
import { usePengoloRuns } from "~/stores/usePengoloRuns";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloLibrary } from "~/stores/usePengoloLibrary";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloModels } from "~/stores/usePengoloModels";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { usePengoloWorkspace } from "~/stores/usePengoloWorkspace";
import { usePengoloTransport } from "~/stores/usePengoloTransport";
import { usePengoloSearch } from "~/stores/usePengoloSearch";
import { usePengoloOpenai } from "~/stores/usePengoloOpenai";
import {
  pengoloOperation,
  invalidatePengoloRequests,
  type WorkspaceIdentity,
} from "~/utils/pengolo-api";
import { invalidatesWorkspace } from "~/utils/pengolo";

export default defineNuxtPlugin((nuxt) => {
  const account = useState<{ subject: string; tenant: string } | null>(
    "account-session",
  );
  const router = useRouter();
  const workspace = usePengoloWorkspace();
  const transport = usePengoloTransport();
  const stores = [
    usePengoloRuns(),
    usePengoloAgents(),
    usePengoloChats(),
    usePengoloLibrary(),
    usePengoloProjects(),
    usePengoloModels(),
    usePengoloUi(),
    workspace,
    transport,
    usePengoloSearch(),
    usePengoloOpenai(),
  ];
  let epoch = 0;
  let identitySubscription: (() => void) | null = null;
  function clear() {
    epoch++;
    identitySubscription = null;
    invalidatePengoloRequests();
    for (const store of stores) store.clear();
    localStorage.removeItem("pengolo_chat_sessions");
  }
  async function preload() {
    if (!account.value || router.currentRoute.value.path === "/login") return;
    const generation = epoch;
    try {
      await workspace.load();
      if (generation !== epoch) return;
      identitySubscription ??= transport.subscribe(
        "identity",
        () => ({ query: pengoloOperation("IdentityChanges") }),
        (data) => {
          const identity = data.pengoloIdentityChanges as WorkspaceIdentity;
          if (generation !== epoch) return;
          if (
            workspace.identity &&
            workspace.identity.version !== identity.version
          ) {
            clear();
            void preload();
          } else {
            usePengoloUi().webAvailable = identity.webSearchAvailable;
            useState("account-administrator", () => false).value =
              identity.administrator;
          }
        },
        (errors) => {
          if (generation !== epoch) return;
          if (
            errors.some(
              (error) => error.extensions?.code === "AUTHENTICATION_EXPIRED",
            )
          )
            transport.reconnect();
          else if (invalidatesWorkspace(errors)) clear();
        },
      );
      const id = router.currentRoute.value.path.match(
        /^\/pengolo\/([a-f0-9]{32})$/,
      )?.[1];
      if (id && workspace.unavailableChat !== id) {
        const chats = usePengoloChats();
        const chat =
          chats.active?.id === id ? chats.active : await chats.open(id);
        if (generation !== epoch) return;
        usePengoloUi().projectId = chat.projectId;
        await usePengoloRuns().resume(chat);
      }
    } catch {
      if (generation === epoch)
        usePengoloUi().error =
          "Pengolo could not load your workspace. Please retry.";
    }
  }
  const stop = watch(
    () => `${account.value?.tenant}/${account.value?.subject}`,
    () => {
      clear();
    },
    { flush: "sync" },
  );
  const stopNavigation = router.afterEach((to, from) => {
    if (to.path === "/login") clear();
    else if (account.value && (from.path === "/login" || !workspace.ready))
      void preload();
  });
  const invalidated = () => {
    clear();
    void preload();
  };
  const refreshed = () => transport.reconnect();
  window.addEventListener("pengolo-access-invalidated", invalidated);
  window.addEventListener("galadril-session-refreshed", refreshed);
  nuxt.hook("app:mounted", () => {
    void preload();
  });
  nuxt.vueApp.onUnmount(() => {
    stop();
    stopNavigation();
    clear();
    window.removeEventListener("pengolo-access-invalidated", invalidated);
    window.removeEventListener("galadril-session-refreshed", refreshed);
  });
});
