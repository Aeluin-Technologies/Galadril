<script setup lang="ts">
import {
  ArrowPathIcon,
  CheckIcon,
  ClockIcon,
  QuestionMarkCircleIcon,
  XMarkIcon,
} from "@heroicons/vue/24/outline";
import {
  projectActivities,
  type AgentQuestion,
  type VisibleQuestion,
  type WarningSource,
} from "~/utils/pengolo-activity";
import type { QuestionResponse } from "~/utils/pengolo-api";

const props = defineProps<{
  events: { sequence: number; kind: string; content: string }[];
  status: string;
  canAnswer: boolean;
  submit: (answer: QuestionResponse) => Promise<void>;
}>();
const activity = computed(() => projectActivities(props.events));
const warningLabels: Record<WarningSource, string> = {
  search:
    "Internal search is unavailable. Continuing with other authorized sources.",
  graph:
    "Some graph evidence is unavailable. Continuing with the evidence that can be accessed.",
  causal: "Causal analysis is unavailable. Continuing without that analysis.",
  library:
    "The document library is unavailable. Continuing with other authorized sources.",
  read_file:
    "A document could not be read. Continuing with available sources; you can upload it again.",
  attachment:
    "An attachment is unavailable. Continuing without its contents; you can upload it again.",
  web: "Public web research is unavailable. Continuing with other available sources.",
  run_python:
    "A calculation could not be completed. Continuing without that result.",
  tool: "An operation could not be completed. Continuing with independent steps; its result has not been assumed.",
};
const drafts = reactive(
  new Map<string, { selected: string[]; custom: string }>(),
);
const sending = reactive(new Set<string>());
const sent = reactive(new Set<string>());
const errors = reactive(new Map<string, string>());
const terminal = computed(() =>
  ["completed", "failed", "cancelled"].includes(props.status),
);
const statusLabel = computed(() => {
  if (terminal.value)
    return props.status === "completed" ? "Work completed" : "Work stopped";
  if (
    activity.value.questions.some(
      (question) => !question.resolved && !sent.has(question.id),
    )
  )
    return props.canAnswer
      ? "Waiting for your answer"
      : "Waiting for the person who started this response";
  if (activity.value.phase?.phase === "writing") return "Writing a response";
  if (activity.value.phase?.phase === "tool") return "Using authorized tools";
  if (activity.value.phase?.phase === "agent") return "An agent is working";
  return "Working through the next step";
});
function draft(id: string, header: string) {
  const key = `${id}:${header}`;
  let value = drafts.get(key);
  if (!value) {
    value = { selected: [], custom: "" };
    drafts.set(key, value);
  }
  return value;
}
function closed(question: VisibleQuestion): boolean {
  return (
    terminal.value ||
    !props.canAnswer ||
    question.resolved ||
    sent.has(question.id) ||
    sending.has(question.id)
  );
}
function choose(id: string, question: AgentQuestion, label: string) {
  const value = draft(id, question.header);
  value.custom = "";
  value.selected = question.multi_select
    ? value.selected.includes(label)
      ? value.selected.filter((item) => item !== label)
      : [...value.selected, label]
    : [label];
}
function custom(id: string, header: string, event: Event) {
  if (!(event.target instanceof HTMLTextAreaElement)) return;
  const value = draft(id, header);
  value.custom = event.target.value;
  value.selected = [];
}
function complete(question: VisibleQuestion): boolean {
  return question.questions.every((item) => {
    const value = drafts.get(`${question.id}:${item.header}`);
    return (
      value !== undefined &&
      (value.custom.trim().length > 0 || value.selected.length > 0)
    );
  });
}
async function respond(question: VisibleQuestion, cancelled = false) {
  if (closed(question) || (!cancelled && !complete(question))) return;
  sending.add(question.id);
  errors.delete(question.id);
  try {
    await props.submit({
      questionId: question.id,
      cancelled,
      answers: cancelled
        ? []
        : question.questions.map((item) => {
            const value = draft(question.id, item.header);
            return {
              header: item.header,
              selected: value.selected,
              customAnswer: value.custom.trim() || null,
            };
          }),
    });
    sent.add(question.id);
  } catch {
    errors.set(
      question.id,
      "Your answer could not be sent. Check that the question is still open, then retry.",
    );
  } finally {
    sending.delete(question.id);
  }
}
</script>

<template>
  <section class="p-agent-activity" aria-label="Agent activity">
    <AiPythonExecutions :executions="activity.python" />
    <p
      v-for="source in activity.warnings"
      :key="source"
      role="status"
      class="p-agent-warning"
    >
      {{ warningLabels[source] }}
    </p>
    <p v-if="activity.billing" class="p-agent-warning" role="status">
      {{
        activity.billing.provider === "chatgpt"
          ? "Using your ChatGPT plan."
          : activity.billing.reason === "quota"
            ? "ChatGPT usage limit reached. Continuing with Galadril's OpenAI API credits."
            : "Using Galadril's OpenAI API credits."
      }}
      <a
        href="https://chatgpt.com/settings/usage"
        target="_blank"
        rel="noopener noreferrer"
        >Manage usage</a
      >
    </p>
    <p class="p-agent-phase" role="status" aria-live="polite">
      <CheckIcon v-if="status === 'completed'" aria-hidden="true" />
      <XMarkIcon v-else-if="terminal" aria-hidden="true" />
      <ArrowPathIcon v-else class="p-agent-spin" aria-hidden="true" />
      {{ statusLabel }}
    </p>
    <section
      v-if="activity.tasks.length"
      class="p-agent-plan"
      aria-label="Execution plan"
    >
      <h3>Plan</h3>
      <TransitionGroup name="p-plan" tag="ol">
        <li
          v-for="task in activity.tasks"
          :key="task.key"
          :data-status="task.status"
          :aria-label="`${task.content}: ${task.status.replaceAll('_', ' ')}`"
        >
          <CheckIcon v-if="task.status === 'completed'" aria-hidden="true" />
          <ArrowPathIcon
            v-else-if="task.status === 'in_progress' && !terminal"
            class="p-agent-spin"
            aria-hidden="true"
          />
          <XMarkIcon
            v-else-if="task.status === 'cancelled'"
            aria-hidden="true"
          />
          <ClockIcon v-else aria-hidden="true" />
          <span
            >{{
              task.status === "in_progress" && task.active_form
                ? task.active_form
                : task.content
            }}<small v-if="task.agentId">{{ task.agentId }}</small></span
          >
        </li>
      </TransitionGroup>
    </section>
    <section
      v-for="question in activity.questions"
      :key="question.id"
      class="p-agent-question"
      :aria-label="`Question ${question.id}`"
    >
      <h3><QuestionMarkCircleIcon aria-hidden="true" />Your input</h3>
      <p
        v-if="question.resolved || sent.has(question.id)"
        class="p-muted"
        role="status"
      >
        {{ question.cancelled ? "Question skipped." : "Answer received." }}
      </p>
      <p v-else-if="terminal" class="p-muted">This question is closed.</p>
      <form v-else @submit.prevent="respond(question)">
        <fieldset
          v-for="item in question.questions"
          :key="item.header"
          :disabled="closed(question)"
        >
          <legend>{{ item.question }}</legend>
          <label
            v-for="option in item.options"
            :key="option.label"
            class="p-agent-option"
          >
            <UiInput
              :type="item.multi_select ? 'checkbox' : 'radio'"
              :name="`${question.id}:${item.header}`"
              :value="option.label"
              :checked="
                drafts
                  .get(`${question.id}:${item.header}`)
                  ?.selected.includes(option.label) ?? false
              "
              @change="choose(question.id, item, option.label)"
            />
            <span
              >{{ option.label
              }}<small v-if="option.description">{{
                option.description
              }}</small></span
            >
          </label>
          <UiTextarea
            :aria-label="`Your answer for ${item.header}`"
            placeholder="Or write your own answer"
            maxlength="4096"
            rows="2"
            :value="drafts.get(`${question.id}:${item.header}`)?.custom ?? ''"
            @input="custom(question.id, item.header, $event)"
          />
        </fieldset>
        <p v-if="errors.get(question.id)" role="alert">
          {{ errors.get(question.id) }}
        </p>
        <div v-if="canAnswer" class="p-agent-answer-actions">
          <UiButton
            type="submit"
            :disabled="closed(question) || !complete(question)"
            variant="secondary"
          >
            <ArrowPathIcon
              v-if="sending.has(question.id)"
              class="p-agent-spin"
              aria-hidden="true"
            />Send answer
          </UiButton>
          <UiButton
            type="button"
            :disabled="closed(question)"
            @click="respond(question, true)"
            variant="secondary"
          >
            Skip question
          </UiButton>
        </div>
      </form>
    </section>
  </section>
</template>

<style scoped>
.p-agent-activity {
  display: grid;
  gap: 0.75rem;
  margin: 0.75rem 0 1rem;
  font-size: 0.875rem;
}
.p-agent-phase {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: var(--p-muted, #737373);
}
.p-agent-warning {
  border-left: 3px solid var(--p-accent, #b96713);
  padding: 0.5rem 0.75rem;
  color: var(--p-muted, #737373);
}
.p-agent-activity svg {
  width: 1rem;
  height: 1rem;
  flex: none;
}
.p-agent-plan,
.p-agent-question {
  border: 1px solid var(--p-border, #dedbd5);
  border-radius: 0.85rem;
  padding: 0.9rem 1rem;
}
.p-agent-activity h3 {
  font-size: 0.875rem;
  font-weight: 600;
  margin: 0 0 0.75rem;
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
.p-agent-plan ol {
  display: grid;
  gap: 0.65rem;
  margin: 0;
  padding: 0;
  list-style: none;
}
.p-agent-plan li {
  display: flex;
  gap: 0.6rem;
  align-items: flex-start;
  transition:
    color 0.2s ease,
    opacity 0.2s ease;
}
.p-agent-plan li[data-status="completed"] {
  color: var(--p-muted, #737373);
}
.p-agent-plan li[data-status="completed"] > svg {
  color: #15803d;
}
.p-agent-activity small {
  display: block;
  font-size: 0.75rem;
  color: var(--p-muted, #737373);
  margin-top: 0.15rem;
}
.p-agent-question fieldset {
  padding: 0;
  margin: 0 0 1rem;
  border: 0;
  display: grid;
  gap: 0.5rem;
  min-width: 0;
}
.p-agent-question legend {
  font-weight: 500;
  margin-bottom: 0.65rem;
}
.p-agent-option {
  display: flex;
  align-items: flex-start;
  gap: 0.6rem;
  border: 1px solid var(--p-border, #dedbd5);
  border-radius: 0.6rem;
  padding: 0.6rem;
  cursor: pointer;
}
.p-agent-option:has(input:checked) {
  border-color: #b77932;
  background: color-mix(in srgb, #b77932 6%, transparent);
}
.p-agent-answer-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.p-agent-answer-actions button {
  display: inline-flex;
  gap: 0.4rem;
  align-items: center;
}
.p-agent-skip {
  background: transparent;
}
.p-agent-spin {
  animation: p-agent-rotate 1.2s linear infinite;
}
.p-plan-enter-active,
.p-plan-leave-active,
.p-plan-move {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}
.p-plan-enter-from,
.p-plan-leave-to {
  opacity: 0;
  transform: translateY(0.3rem);
}
@keyframes p-agent-rotate {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .p-agent-spin {
    animation: none;
  }
  .p-agent-plan li,
  .p-plan-enter-active,
  .p-plan-leave-active,
  .p-plan-move {
    transition: none;
  }
}
</style>
