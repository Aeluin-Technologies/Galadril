import { usePengoloRuns } from "~/stores/usePengoloRuns";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloLibrary } from "~/stores/usePengoloLibrary";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloModels } from "~/stores/usePengoloModels";
import { usePengoloUi } from "~/stores/usePengoloUi";
/** An authorization change invalidates every workspace cache as one boundary. */
import { pengoloRequest, invalidatePengoloRequests } from "~/utils/pengolo-api";

export default defineNuxtPlugin((nuxt) => {
  const account = useState<{ subject: string; tenant: string } | null>(
    "account-session",
  );
  const route = useRouter();
  let identityEpoch = 0;
  const stores = [
    usePengoloRuns(),
    usePengoloChats(),
    usePengoloLibrary(),
    usePengoloProjects(),
    usePengoloModels(),
    usePengoloUi(),
  ];
  const clear = () => {
    invalidatePengoloRequests();
    for (const store of stores) store.clear();
    localStorage.removeItem("pengolo_chat_sessions");
  };
  const preload = async () => {
    const epoch = identityEpoch;
    await Promise.allSettled([
      usePengoloModels().load(),
      usePengoloChats().load(),
      usePengoloProjects().load(),
    ]);
    if (epoch !== identityEpoch) return;
    const id = route.currentRoute.value.path.match(
      /^\/pengolo\/([a-f0-9]{32})$/,
    )?.[1];
    if (id) {
      try {
        const chat = await usePengoloChats().open(id, false);
        usePengoloUi().projectId = chat.projectId;
        await usePengoloRuns().resume(chat, false);
      } catch {
        if (epoch === identityEpoch)
          usePengoloUi().error =
            "This conversation is no longer available to your account.";
      }
    }
  };
  let version = "";
  let checking = false;
  const check = async () => {
    if (
      checking ||
      !account.value ||
      document.hidden ||
      route.currentRoute.value.path === "/login"
    )
      return;
    checking = true;
    const epoch = identityEpoch;
    try {
      const identity = await pengoloRequest<{
        version: string;
        webSearchAvailable?: boolean;
      }>("query { pengoloIdentity }");
      if (epoch !== identityEpoch) return;
      const changed = version !== identity.version;
      if (version && changed) {
        clear();
        void preload();
      }
      version = identity.version;
      usePengoloUi().webAvailable = identity.webSearchAvailable === true;
      if (changed) window.dispatchEvent(new Event("pengolo-identity-verified"));
    } catch {
      if (epoch === identityEpoch) clear();
    } finally {
      checking = false;
      if (epoch !== identityEpoch) void check();
    }
  };
  const stop = watch(
    () =>
      `${account.value?.tenant}/${account.value?.subject}/${route.currentRoute.value.path === "/login"}`,
    () => {
      identityEpoch++;
      version = "";
      clear();
      if (account.value && route.currentRoute.value.path !== "/login") {
        void preload();
        void check();
      }
    },
  );
  const timer = window.setInterval(() => void check(), 10_000);
  window.addEventListener("pengolo-access-invalidated", clear);
  clear();
  nuxt.hook("app:mounted", () => {
    void check();
  });
  nuxt.vueApp.onUnmount(() => {
    stop();
    clearInterval(timer);
    window.removeEventListener("pengolo-access-invalidated", clear);
    clear();
  });
});
