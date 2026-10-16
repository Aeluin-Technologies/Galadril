interface Operation {
  query: string;
  variables?: Record<string, unknown>;
}
interface Failure {
  extensions?: { code?: string };
  message?: string;
}
interface Subscription {
  operation: () => Operation;
  next: (data: Record<string, unknown>) => void;
  error: (errors: Failure[]) => void;
}
type Socket = Pick<
  WebSocket,
  | "readyState"
  | "onopen"
  | "onclose"
  | "onerror"
  | "onmessage"
  | "send"
  | "close"
>;

/** Cursors are read at subscription time, including every reconnect. */
export function createPengoloSocket(
  url: string,
  SocketClass: new (url: string, protocol: string) => Socket = WebSocket,
) {
  const subscriptions = new Map<string, Subscription>();
  let socket: Socket | null = null;
  let acknowledged = false;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let deadline: ReturnType<typeof setTimeout> | null = null;
  let attempts = 0;
  function disconnect() {
    if (retry) clearTimeout(retry);
    if (deadline) clearTimeout(deadline);
    retry = null;
    deadline = null;
    const previous = socket;
    socket = null;
    acknowledged = false;
    if (previous) {
      previous.onclose = null;
      previous.close();
    }
  }
  function send(id: string, subscription: Subscription) {
    socket?.send(
      JSON.stringify({
        id,
        type: "subscribe",
        payload: subscription.operation(),
      }),
    );
  }
  function connect() {
    if (socket || !subscriptions.size) return;
    const current = new SocketClass(url, "graphql-transport-ws");
    socket = current;
    current.onopen = () =>
      current.send(JSON.stringify({ type: "connection_init" }));
    deadline = setTimeout(() => current.close(), 10_000);
    current.onmessage = (event) => {
      if (current !== socket) return;
      try {
        const frame = JSON.parse(String(event.data)) as {
          type: string;
          id?: string;
          payload?:
            { data?: Record<string, unknown>; errors?: Failure[] } | Failure[];
        };
        if (frame.type === "ping") {
          current.send(JSON.stringify({ type: "pong" }));
          return;
        }
        if (frame.type === "connection_ack") {
          acknowledged = true;
          attempts = 0;
          if (deadline) clearTimeout(deadline);
          deadline = null;
          for (const [id, subscription] of subscriptions)
            send(id, subscription);
          return;
        }
        const subscription = frame.id ? subscriptions.get(frame.id) : undefined;
        if (!subscription) return;
        if (frame.type === "error") {
          subscription.error(Array.isArray(frame.payload) ? frame.payload : []);
          current.close();
        } else if (
          frame.type === "next" &&
          frame.payload &&
          !Array.isArray(frame.payload)
        ) {
          if (frame.payload.errors?.length)
            subscription.error(frame.payload.errors);
          else if (frame.payload.data) subscription.next(frame.payload.data);
        } else if (frame.type === "complete") {
          subscriptions.delete(frame.id!);
          if (!subscriptions.size) disconnect();
        }
      } catch {
        current.close();
      }
    };
    current.onerror = () => current.close();
    current.onclose = () => {
      if (current !== socket) return;
      socket = null;
      acknowledged = false;
      if (deadline) clearTimeout(deadline);
      deadline = null;
      if (subscriptions.size)
        retry = setTimeout(
          connect,
          Math.min(30_000, 500 * 2 ** Math.min(attempts++, 6)),
        );
    };
  }
  function subscribe(
    id: string,
    operation: Subscription["operation"],
    next: Subscription["next"],
    error: Subscription["error"],
  ) {
    const subscription = { operation, next, error };
    subscriptions.set(id, subscription);
    if (acknowledged) send(id, subscription);
    else connect();
    return () => {
      if (subscriptions.get(id) !== subscription) return;
      subscriptions.delete(id);
      if (acknowledged) socket?.send(JSON.stringify({ id, type: "complete" }));
      if (!subscriptions.size) disconnect();
    };
  }
  return {
    subscribe,
    reconnect() {
      disconnect();
      connect();
    },
    clear() {
      subscriptions.clear();
      disconnect();
    },
  };
}
