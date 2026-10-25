<script setup lang="ts">
import { VueFlow } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { Controls } from "@vue-flow/controls";
import { SpanStatusCode, trace } from "@opentelemetry/api";
import { pengoloData } from "~/utils/pengolo-api";
import {
  parseEmailThread,
  relatedEmailExecutions,
  type EmailThread,
  type EmailExecution,
} from "~/utils/email-thread";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import "@vue-flow/controls/dist/style.css";

const props = defineProps<{ account: string; messageId: string }>();
const emit = defineEmits<{ close: []; read: [id: string] }>();
const { t } = useI18n();
const api = useEmailApi();
const identity = useAccount();
const thread = shallowRef<EmailThread>(),
  executions = shallowRef<EmailExecution[]>([]);
const names = shallowRef(new Map<string, string>());
const loading = ref(false),
  failed = ref(false),
  tracesFailed = ref(false),
  selected = ref("");
let epoch = 0;
let controller: AbortController | undefined;
function stop() {
  epoch++;
  api.cancel();
  controller?.abort();
  controller = undefined;
}
async function load() {
  stop();
  const own = epoch;
  controller = new AbortController();
  loading.value = true;
  failed.value = false;
  tracesFailed.value = false;
  const span = trace.getTracer("galadril.dashboard").startSpan("email.thread");
  try {
    const loaded = parseEmailThread(
      await api.operation({
        operation: "thread",
        account: props.account,
        message: props.messageId,
      }),
    );
    if (own !== epoch) return;
    thread.value = loaded;
    try {
      const result = await pengoloData<{
        pipelineExecutions: EmailExecution[];
        pipelineLinkTargets: Array<{ pipelineId: string; name: string }>;
      }>(
        "query EmailThreadPipelines { pipelineExecutions(limit:100) { commandId pipelineId step status correlationId sourceEmailAccountId sourceEmailId sourceStoragePath } pipelineLinkTargets(limit:100) { pipelineId name } }",
        {},
        false,
        controller.signal,
      );
      if (own !== epoch) return;
      executions.value = Array.isArray(result.pipelineExecutions)
        ? result.pipelineExecutions.slice(0, 100)
        : [];
      names.value = new Map(
        (result.pipelineLinkTargets ?? []).map((item) => [
          item.pipelineId,
          item.name,
        ]),
      );
    } catch {
      if (own === epoch) {
        tracesFailed.value = true;
        span.setStatus({ code: SpanStatusCode.ERROR });
      }
    }
  } catch {
    if (own === epoch) {
      failed.value = true;
      span.setStatus({ code: SpanStatusCode.ERROR });
    }
  } finally {
    if (own === epoch) loading.value = false;
    span.end();
  }
}
interface GraphNode {
  id: string;
  label: string;
  position: { x: number; y: number };
  class: string;
  style: { width: string };
  data: { title: string; detail: string };
}
const graph = computed(() => {
  const nodes: GraphNode[] = [],
    edges: Array<{ id: string; source: string; target: string }> = [];
  const messages = thread.value?.messages ?? [];
  const ids = new Map(
    messages.map((message) => [
      message.message_id,
      `message:${message.number}`,
    ]),
  );
  let y = 0;
  const add = (
    id: string,
    title: string,
    detail: string,
    x: number,
    top: number,
  ) =>
    nodes.push({
      id,
      label: title,
      position: { x, y: top },
      class: "email-thread-node",
      style: { width: "240px" },
      data: { title, detail },
    });
  const connect = (source: string, target: string) =>
    edges.push({ id: `${source}>${target}`, source, target });
  for (const message of messages) {
    const id = `message:${message.number}`;
    add(id, `#${message.number} · ${message.subject}`, message.reference, 0, y);
    const parent = ids.get(message.parent);
    if (parent && parent !== id) connect(parent, id);
    if (message.ingested) {
      add(
        `ingestion:${id}`,
        t("email.thread.ingestion"),
        message.source_key,
        320,
        y,
      );
      connect(id, `ingestion:${id}`);
    }
    if (message.model) {
      add(
        `analysis:${id}`,
        t("email.thread.analysis"),
        `${message.model}\n${message.analysis}`,
        640,
        y,
      );
      connect(id, `analysis:${id}`);
    }
    const traces = relatedEmailExecutions(executions.value, props.account, [
      message,
    ]);
    traces.forEach((execution, index) => {
      const node = `pipeline:${message.number}:${execution.commandId}`;
      add(
        node,
        `${names.value.get(execution.pipelineId) ?? t("email.thread.pipeline")} · ${execution.step}`,
        `${execution.status}\n${execution.pipelineId}\n${execution.correlationId}`,
        960,
        y + index * 100,
      );
      connect(message.ingested ? `ingestion:${id}` : id, node);
    });
    y += Math.max(180, traces.length * 100);
  }
  return { nodes, edges };
});
const selectedNode = computed(() =>
  graph.value.nodes.find((node) => node.id === selected.value),
);
watch(
  () => [props.account, props.messageId],
  () => {
    thread.value = undefined;
    executions.value = [];
    selected.value = "";
    void load();
  },
  { immediate: true },
);
watch(
  () => [identity.session.value?.tenant, identity.session.value?.subject],
  () => {
    stop();
    thread.value = undefined;
    emit("close");
  },
);
onBeforeUnmount(stop);
onDeactivated(stop);
</script>

<template>
  <UiDialog
    :open="true"
    :title="$t('email.thread.title')"
    size="wide"
    :close-label="$t('actions.close')"
    @close="$emit('close')"
  >
    <div class="space-y-5">
      <p v-if="loading" role="status">{{ $t("email.thread.loading") }}</p>
      <UiAlert v-if="failed" tone="danger"
        >{{ $t("email.thread.failed")
        }}<UiButton variant="ghost" :action="load">{{
          $t("email.retry")
        }}</UiButton></UiAlert
      >
      <template v-if="thread">
        <p class="text-sm text-muted">
          {{ $t("email.thread.root") }}
          <code class="break-all">{{ thread.thread_id }}</code>
        </p>
        <p class="text-sm text-muted">
          {{ $t("email.thread.pipelineHistory") }}
        </p>
        <UiAlert v-if="tracesFailed" tone="info">{{
          $t("email.thread.tracesUnavailable")
        }}</UiAlert>
        <UiAlert v-if="!thread.indexed_complete || thread.truncated" tone="info"
          >{{ $t("email.thread.partial")
          }}<UiButton variant="ghost" :action="load">{{
            $t("email.thread.continueIndexing")
          }}</UiButton></UiAlert
        >
        <div
          class="h-96 overflow-hidden rounded-panel border border-border bg-canvas"
          role="region"
          :aria-label="$t('email.thread.graph')"
        >
          <VueFlow
            v-if="graph.nodes.length"
            :nodes="graph.nodes"
            :edges="graph.edges"
            :nodes-draggable="false"
            :nodes-connectable="false"
            fit-view-on-init
            :min-zoom="0.15"
            :max-zoom="1.5"
            @node-click="({ node }) => (selected = node.id)"
            ><Background /><Controls :show-interactive="false"
          /></VueFlow>
        </div>
        <UiCard v-if="selectedNode" :title="selectedNode.data.title">
          <pre class="whitespace-pre-wrap break-all font-sans text-sm">{{
            selectedNode.data.detail
          }}</pre>
        </UiCard>
        <ul
          class="max-h-64 divide-y divide-border overflow-y-auto"
          :aria-label="$t('email.thread.messages')"
        >
          <li
            v-for="message in thread.messages"
            :key="message.number"
            class="py-3"
          >
            <UiButton
              v-if="message.received_id"
              variant="ghost"
              align="start"
              class="w-full"
              @click="$emit('read', message.received_id)"
              >#{{ message.number }} · {{ message.subject }}</UiButton
            >
            <p v-else class="font-medium">
              #{{ message.number }} · {{ message.subject }}
            </p>
            <code class="block break-all text-xs text-muted">{{
              message.reference
            }}</code>
          </li>
        </ul>
      </template>
    </div>
  </UiDialog>
</template>

<style scoped>
:deep(.email-thread-node) {
  color: var(--foreground);
  background: var(--surface);
  border-color: var(--border-control);
  overflow-wrap: anywhere;
}
</style>
