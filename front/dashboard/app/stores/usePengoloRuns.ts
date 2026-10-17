import { defineStore } from "pinia";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloTransport } from "~/stores/usePengoloTransport";
import {
  isTerminalStatus as terminal,
  mergeEvents,
  mergeMessages,
  replayStatus,
  invalidatesWorkspace,
} from "~/utils/pengolo";
import {
  pengoloMutation,
  pengoloRequest,
  pengoloOperation,
  type PengoloEvent,
  type PengoloMessage,
  type ChatRecord,
  type GenerationRecord,
  type GenerationReplay,
  type QuestionResponse,
} from "~/utils/pengolo-api";

interface Run {
  id: string;
  chatId: string;
  temporary: boolean;
  status: string;
  events: PengoloEvent[];
  text: string;
  error: string;
  canAnswer: boolean;
}

export const usePengoloRuns = defineStore("pengoloRuns", () => {
  const runs = ref<Record<string, Run>>({});
  const sending = ref(false);
  const subscriptions = new Map<string, () => void>();
  let epoch = 0;
  function close(id: string) {
    subscriptions.get(id)?.();
    subscriptions.delete(id);
  }
  function apply(run: Run, page: GenerationReplay) {
    const cursor = run.events.at(-1)?.sequence ?? 0;
    run.events = mergeEvents(run.events, page.events);
    run.canAnswer = page.canAnswer === true;
    for (const event of page.events)
      if (event.sequence > cursor && event.kind === "content")
        run.text += event.content;
    run.status = replayStatus(run.status, page.status, run.events);
    run.error =
      run.status === "failed"
        ? "Generation failed. Your message has been kept. You can retry."
        : "";
    if (terminal(run.status)) {
      close(run.id);
      usePengoloChats().finish(run.chatId, page, run.temporary);
    }
  }
  async function replay(run: Run) {
    const generation = epoch;
    const page = await pengoloRequest<GenerationReplay>(
      pengoloOperation("Events"),
      {
        id: run.id,
        after: run.events.at(-1)?.sequence ?? 0,
        temporary: run.temporary,
      },
    );
    if (generation === epoch) apply(run, page);
  }
  function subscribe(run: Run) {
    if (terminal(run.status) || subscriptions.has(run.id)) return;
    const generation = epoch;
    const unsubscribe = usePengoloTransport().subscribe(
      run.id,
      () => ({
        query: pengoloOperation("Stream"),
        variables: {
          id: run.id,
          after: run.events.at(-1)?.sequence ?? 0,
          temporary: run.temporary,
        },
      }),
      (data) => {
        if (generation !== epoch) return;
        try {
          apply(run, data.pengoloStream as GenerationReplay);
        } catch {
          run.error = "Connection interrupted. Reconnecting…";
          usePengoloTransport().reconnect();
        }
      },
      (errors) => {
        if (generation !== epoch) return;
        if (
          errors.some(
            (error) => error.extensions?.code === "AUTHENTICATION_EXPIRED",
          )
        )
          usePengoloTransport().reconnect();
        else if (invalidatesWorkspace(errors))
          window.dispatchEvent(new Event("pengolo-access-invalidated"));
        else {
          run.error = "Connection interrupted. Reconnecting…";
          usePengoloTransport().reconnect();
        }
      },
    );
    subscriptions.set(run.id, unsubscribe);
  }
  async function send(input: Record<string, unknown>) {
    if (sending.value) return null;
    const generation = epoch;
    sending.value = true;
    try {
      const result = await pengoloMutation<{
        chat: ChatRecord;
        generation: GenerationRecord;
        userMessage: PengoloMessage | null;
      }>("sendPengolo", input);
      if (generation !== epoch) return null;
      const chats = usePengoloChats();
      const previous =
        chats.active?.id === result.chat.id
          ? (chats.active.data.messages ?? [])
          : [];
      result.chat.data.messages = result.userMessage
        ? mergeMessages(previous, [result.userMessage])
        : previous;
      chats.upsert(result.chat, input.temporary === true, true);
      const run = reactive<Run>({
        id: result.generation.id,
        chatId: result.chat.id,
        temporary: input.temporary === true,
        // Delivery is complete only after cursor replay reaches the durable terminal message.
        status: "pending",
        events: [],
        text: "",
        error: "",
        canAnswer: true,
      });
      runs.value[run.id] = run;
      subscribe(run);
      return result;
    } finally {
      if (generation === epoch) sending.value = false;
    }
  }
  async function resume(chat: ChatRecord, temporary = false) {
    const id = chat.data.activeGeneration;
    if (!id || runs.value[id]) return;
    const run = reactive<Run>({
      id,
      chatId: chat.id,
      temporary,
      status: "running",
      events: [],
      text: "",
      error: "",
      canAnswer: false,
    });
    runs.value[id] = run;
    subscribe(run);
  }
  async function answer(id: string, response: QuestionResponse) {
    const run = runs.value[id];
    if (!run || !run.canAnswer || terminal(run.status))
      throw new Error("Question unavailable");
    await pengoloMutation("answerPengoloQuestion", {
      id,
      temporary: run.temporary,
      ...response,
    });
    await replay(run);
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
    for (const id of subscriptions.keys()) close(id);
    runs.value = {};
    sending.value = false;
  }
  return { runs, sending, send, resume, stop, answer, forgetTemporary, clear };
});
