import { defineStore } from "pinia";
import {
  pengoloMutation,
  pengoloOperation,
  pengoloRequest,
} from "~/utils/pengolo-api";
import { chatgptAuthorizationUrl } from "~/utils/pengolo";
import { usePengoloModels } from "~/stores/usePengoloModels";

export interface OpenaiAccount {
  id: string;
  label: string;
  connected: boolean;
}
interface OpenaiStatus {
  enabled: boolean;
  connected: boolean;
  quotaExhausted: boolean;
  selected: string | null;
  accounts: OpenaiAccount[];
  authorizationUrl?: string | null;
  revocationConfirmed?: boolean | null;
}

export const usePengoloOpenai = defineStore("pengoloOpenai", () => {
  const status = ref<OpenaiStatus | null>(null);
  const busy = ref(false);
  const error = ref("");
  const notice = ref("");
  let epoch = 0;
  let poll: ReturnType<typeof setTimeout> | null = null;

  function stop() {
    if (poll !== null) clearTimeout(poll);
    poll = null;
  }
  function clear() {
    epoch++;
    stop();
    status.value = null;
    busy.value = false;
    error.value = "";
    notice.value = "";
  }
  async function refresh() {
    const current = epoch;
    const value = await pengoloRequest<OpenaiStatus>(
      pengoloOperation("PengoloOpenaiAccount"),
    );
    if (current !== epoch || !value || typeof value.enabled !== "boolean")
      return;
    status.value = value;
  }
  async function initialize() {
    const current = epoch;
    try {
      await refresh();
    } catch {
      if (current === epoch)
        error.value =
          "Your connection settings could not be loaded. Reload this page to retry.";
    }
  }
  async function update(
    operation: "CONNECT" | "SELECT" | "DISCONNECT" | "RETRY",
    registration: string | null = null,
  ) {
    if (busy.value) return;
    const current = epoch;
    const popup =
      operation === "CONNECT"
        ? window.open(
            "about:blank",
            "galadril-chatgpt",
            "popup,width=600,height=800",
          )
        : null;
    if (popup) popup.opener = null;
    busy.value = true;
    error.value = "";
    notice.value = "";
    stop();
    try {
      if (operation === "CONNECT" && !popup)
        throw new Error("Popup unavailable");
      const result = await pengoloMutation<OpenaiStatus>(
        "updatePengoloOpenai",
        { operation, registration },
      );
      if (current !== epoch) {
        popup?.close();
        return;
      }
      const { authorizationUrl, ...metadata } = result;
      status.value = metadata;
      if (operation === "CONNECT") {
        const url = chatgptAuthorizationUrl(authorizationUrl);
        if (!url || !popup) throw new Error("Invalid authorization endpoint");
        popup.location.replace(url);
        const deadline = Date.now() + 300_000;
        const before = metadata.accounts
          .map((account) => `${account.id}:${account.connected}`)
          .join("|");
        const check = async () => {
          if (current !== epoch) return;
          try {
            await refresh();
            if (current !== epoch) return;
            const after = status.value?.accounts
              .map((account) => `${account.id}:${account.connected}`)
              .join("|");
            if (status.value?.connected && before !== after) {
              stop();
              await usePengoloModels().refresh(true);
              if (current !== epoch) return;
              notice.value =
                "Your ChatGPT plan is connected. Eligible requests use your shared plan allowance.";
              return;
            }
          } catch {
            /* A transient status failure must not consume the OAuth attempt. */
          }
          if (current !== epoch) return;
          if (Date.now() >= deadline) {
            error.value =
              "Connection was not confirmed. You can start a new connection.";
            return;
          }
          poll = setTimeout(() => {
            void check();
          }, 2000);
        };
        poll = setTimeout(() => {
          void check();
        }, 500);
      } else {
        await usePengoloModels().refresh(true);
        if (current !== epoch) return;
        notice.value =
          operation === "DISCONNECT" && result.revocationConfirmed === false
            ? "Disconnected here. Remote revocation was not confirmed; you can also disconnect Galadril in ChatGPT settings."
            : operation === "DISCONNECT"
              ? "ChatGPT account disconnected."
              : "ChatGPT account preference updated.";
      }
    } catch {
      popup?.close();
      if (current === epoch)
        error.value =
          "The ChatGPT connection could not be updated. Allow the sign-in window, check your local callback, then retry.";
    } finally {
      if (current === epoch) busy.value = false;
    }
  }
  return {
    status,
    busy,
    error,
    notice,
    initialize,
    refresh,
    update,
    stop,
    clear,
  };
});
