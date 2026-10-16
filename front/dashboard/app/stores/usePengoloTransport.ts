import { defineStore } from "pinia";
import { createPengoloSocket } from "~/utils/pengolo-socket";

export const usePengoloTransport = defineStore("pengoloTransport", () => {
  let socket: ReturnType<typeof createPengoloSocket> | null = null;
  function transport() {
    if (!socket) {
      const url = new URL("/graphql", window.location.origin);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      socket = createPengoloSocket(url.href);
    }
    return socket;
  }
  function subscribe(
    ...arguments_: Parameters<
      ReturnType<typeof createPengoloSocket>["subscribe"]
    >
  ) {
    return transport().subscribe(...arguments_);
  }
  function reconnect() {
    socket?.reconnect();
  }
  function clear() {
    socket?.clear();
    socket = null;
  }
  return { subscribe, reconnect, clear };
});
