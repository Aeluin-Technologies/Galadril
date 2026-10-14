import { usePengoloChats } from "~/stores/usePengoloChats";
import { defineStore } from "pinia";
import { mergeEvents } from "~/utils/pengolo";
import {
  pengoloMutation,
  pengoloRequest,
  type PengoloEvent,
  type ChatRecord,
  type GenerationRecord,
} from "~/utils/pengolo-api";

interface Run {
  id: string;
  chatId: string;
  temporary: boolean;
  status: string;
  events: PengoloEvent[];
  error: string;
}
interface GraphqlFrame {
  type: string;
  payload?: {
    errors?: Array<{ extensions?: { code?: string } }>;
    data?: { pengoloStream?: string | Replay };
  };
}
interface Replay {
  events: PengoloEvent[];
  status: string;
}
const terminal = (status: string) =>
  ["completed", "failed", "cancelled"].includes(status);

export const usePengoloRuns = defineStore("pengoloRuns", () => {
  const runs = ref<Record<string, Run>>({});
  const sending = ref(false);
  const sockets = new Map<string, WebSocket>();
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  let epoch = 0;

  function close(id: string) {
    const socket = sockets.get(id);
    sockets.delete(id);
    if (socket) {
      socket.onclose = null;
      socket.close();
    }
    const timer = timers.get(id);
    if (timer) clearTimeout(timer);
    timers.delete(id);
  }
  async function apply(run: Run, page: Replay) {
    const generation = epoch;
    run.events = mergeEvents(run.events, page.events);
    run.status = page.status;
    run.error =
      page.status === "failed"
        ? "Generation failed. Your message has been kept."
        : "";
    if (terminal(run.status)) {
      close(run.id);
      const chats = usePengoloChats();
      const chat = await chats.open(run.chatId, run.temporary, false);
      if (generation === epoch) chats.upsert(chat, run.temporary);
    }
  }
  async function replay(run: Run) {
    const generation = epoch;
    const page = await pengoloRequest<Replay>(
      "query Replay($id: String!, $after: Int!, $temporary: Boolean!) { pengoloEvents(generationId: $id, after: $after, temporary: $temporary) }",
      {
        id: run.id,
        after: run.events.at(-1)?.sequence ?? 0,
        temporary: run.temporary,
      },
    );
    if (generation === epoch) await apply(run, page);
  }
  function subscribe(run: Run, attempts = 0) {
    close(run.id);
    if (terminal(run.status)) return;
    const generation = epoch;
    const url = new URL("/graphql", window.location.origin);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url, "graphql-transport-ws");
    sockets.set(run.id, socket);
    let acknowledged = false;
    const reconnect = async () => {
      if (generation !== epoch || terminal(run.status)) return;
      try {
        await replay(run);
      } catch {
        run.error = "Connection interrupted. Reconnecting…";
      }
      if (generation === epoch && !terminal(run.status))
        timers.set(
          run.id,
          setTimeout(
            () => subscribe(run, attempts + 1),
            Math.min(30_000, 1000 * 2 ** Math.min(attempts, 5)),
          ),
        );
    };
    socket.onopen = () =>
      socket.send(JSON.stringify({ type: "connection_init" }));
    socket.onmessage = (incoming) => {
      if (generation !== epoch) return;
      try {
        const frame = JSON.parse(String(incoming.data)) as GraphqlFrame;
        if (frame.type === "ping") {
          socket.send(JSON.stringify({ type: "pong" }));
          return;
        }
        if (frame.type === "pong") return;
        if (frame.type === "connection_ack") {
          acknowledged = true;
          socket.send(
            JSON.stringify({
              id: run.id,
              type: "subscribe",
              payload: {
                query:
                  "subscription Replay($id: String!, $after: Int!, $temporary: Boolean!) { pengoloStream(generationId: $id, after: $after, temporary: $temporary) }",
                variables: {
                  id: run.id,
                  after: run.events.at(-1)?.sequence ?? 0,
                  temporary: run.temporary,
                },
              },
            }),
          );
        } else if (frame.type === "next") {
          if (frame.payload?.errors?.length)
            throw new Error("Subscription rejected");
          const raw = frame.payload?.data?.pengoloStream;
          const page =
            typeof raw === "string" ? (JSON.parse(raw) as Replay) : raw;
          if (!page) throw new Error("Invalid replay frame");
          void apply(run, page).catch(() => {
            run.error = "Unable to refresh this conversation.";
          });
        } else if (frame.type === "error") {
          socket.close();
        } else if (frame.type === "complete" && !terminal(run.status))
          socket.close();
      } catch {
        socket.close();
      }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      sockets.delete(run.id);
      void reconnect();
    };
    timers.set(
      run.id,
      setTimeout(() => {
        if (!acknowledged) socket.close();
      }, 10_000),
    );
  }
  async function send(input: Record<string, unknown>) {
    if (sending.value) return null;
    const generation = epoch;
    sending.value = true;
    try {
      const result = await pengoloMutation<{
        chat: ChatRecord;
        generation: GenerationRecord;
      }>("sendPengolo", input);
      if (generation !== epoch) return null;
      usePengoloChats().upsert(result.chat, input.temporary === true, true);
      const run = reactive<Run>({
        id: result.generation.id,
        chatId: result.chat.id,
        temporary: input.temporary === true,
        status: String(result.generation.data.status),
        events: [],
        error: "",
      });
      runs.value[run.id] = run;
      void replay(run)
        .catch(() => {
          run.error = "Connection interrupted. Reconnecting…";
        })
        .finally(() => {
          if (generation === epoch) subscribe(run);
        });
      return result;
    } finally {
      if (generation === epoch) sending.value = false;
    }
  }
  async function resume(chat: ChatRecord, temporary = false) {
    const id = chat.data.activeGeneration;
    if (typeof id !== "string" || runs.value[id]) return;
    const run = reactive<Run>({
      id,
      chatId: chat.id,
      temporary,
      status: "running",
      events: [],
      error: "",
    });
    runs.value[id] = run;
    await replay(run);
    subscribe(run);
  }
  async function stop(id: string) {
    const run = runs.value[id];
    if (!run) return;
    await pengoloMutation("cancelPengolo", { id, temporary: run.temporary });
    await replay(run);
  }
  function forgetTemporary(chatId: string) {
    for (const run of Object.values(runs.value))
      if (run.temporary && run.chatId === chatId) {
        close(run.id);
        delete runs.value[run.id];
      }
  }
  function clear() {
    epoch++;
    for (const id of sockets.keys()) close(id);
    for (const timer of timers.values()) clearTimeout(timer);
    timers.clear();
    runs.value = {};
    sending.value = false;
  }
  return { runs, sending, send, resume, stop, forgetTemporary, clear };
});
