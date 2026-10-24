<script setup lang="ts">
const { t, locale } = useI18n();
useHead(() => ({ htmlAttrs: { lang: locale.value } }));

import { usePengoloAgents } from "~/stores/usePengoloAgents";
import {
  Bars3Icon,
  XMarkIcon,
  PlusIcon,
  ClockIcon,
  BookOpenIcon,
  ChevronDownIcon,
  PencilSquareIcon,
  RectangleGroupIcon,
} from "@heroicons/vue/24/outline";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloModels } from "~/stores/usePengoloModels";
import { usePengoloRuns } from "~/stores/usePengoloRuns";
import { usePengoloWorkspace } from "~/stores/usePengoloWorkspace";
import type {
  PengoloMessage,
  ProjectRecord,
  FileRecord,
} from "~/utils/pengolo-api";
const props = defineProps<{
  view?: "chat" | "library" | "scheduled";
  chatId?: string;
  compact?: boolean;
}>();
const route = useRoute();
const router = useRouter();
const account = useState<{ subject: string; tenant: string } | null>(
  "account-session",
);
const ui = usePengoloUi();
const chats = usePengoloChats();
const projects = usePengoloProjects();
const models = usePengoloModels();
const runs = usePengoloRuns();
const workspace = usePengoloWorkspace();
const creatingProject = ref(false);
const editingProject = ref<ProjectRecord | null>(null);
const projectsExpanded = ref(true);
const chatsExpanded = ref(true);
const scroller = ref<HTMLElement | null>(null);
const followsBottom = ref(true);
const opening = ref(false);
let openingRequest = 0;
const pending = ref<{ serialized: string; id: string } | null>(null);
const project = computed(() =>
  projects.items.find(
    (item) => item.id === (chats.active?.projectId ?? ui.projectId),
  ),
);
const activeRun = computed(() =>
  Object.values(runs.runs).find(
    (run) =>
      run.chatId === chats.active?.id &&
      !["completed", "failed", "cancelled"].includes(run.status),
  ),
);
const activityRun = computed(
  () =>
    activeRun.value ??
    Object.values(runs.runs).findLast(
      (run) =>
        run.chatId === chats.active?.id &&
        run.events.some((event) => event.kind === "activity"),
    ),
);
const busy = computed(() => !!activeRun.value || runs.sending);
const messages = computed<PengoloMessage[]>(
  () => chats.active?.data.messages ?? [],
);
const streamingText = computed(() => activeRun.value?.text ?? "");
const toolNames = computed<Record<string, string>>(() => ({
  context: t("pengolo.copy.exploring_graph_context"),
  web: t("pengolo.copy.searching_the_web"),
  search: t("pengolo.copy.searching_galadril"),
  graph: t("pengolo.copy.following_graph_relationships"),
  causal: t("pengolo.copy.analyzing_relationships"),
  library: t("pengolo.copy.reading_library_documents"),
  run_python: t("pengolo.copy.running_calculations"),
  mcp: t("pengolo.copy.using_a_connected_tool"),
}));
const toolStatus = computed(() => {
  const event = activeRun.value?.events
    .filter((event) => event.kind === "tool")
    .at(-1);
  return event
    ? (toolNames.value[event.content] ?? t("pengolo.authorized_tools"))
    : t("pengolo.working");
});
const libraryScope = ref("");
watch(
  () => ui.draftVersion,
  () => {
    creatingProject.value = false;
    editingProject.value = null;
    pending.value = null;
  },
);

async function toggleProject(event: Event, id: string) {
  if (event.target instanceof HTMLDetailsElement && event.target.open) {
    try {
      await chats.load(false, id);
    } catch {
      ui.error = t("pengolo.copy.project_conversations_could_not_be_loaded");
    }
  }
}
onBeforeRouteLeave(async (to) => {
  if (ui.temporary && to.path !== route.path) await ui.closeTemporary();
});
async function initialize() {
  const version = ui.draftVersion;
  try {
    await workspace.load();
    if (props.chatId === chats.active?.id && !opening.value) {
      followsBottom.value = true;
      await bottom();
    }
  } catch {
    if (version === ui.draftVersion)
      ui.error = t(
        "pengolo.copy.pengolo_could_not_load_your_workspace_please_retry",
      );
  }
}
async function openChat(id: string) {
  const version = ui.draftVersion;
  const request = ++openingRequest;
  opening.value = true;
  try {
    ui.error = "";
    await workspace.load();
    if (workspace.unavailableChat === id)
      throw new Error("Conversation unavailable");
    const chat = await chats.open(id, ui.temporary);
    if (version !== ui.draftVersion || request !== openingRequest) return;
    ui.projectId = chat.projectId;
    ui.graphContext = chat.data.generationContext?.graphContext ?? null;
    usePengoloAgents().selected = [
      ...(chat.data.generationContext?.agentIds ?? []),
    ];
    await runs.resume(chat, ui.temporary);
    opening.value = false;
    followsBottom.value = true;
    await bottom();
  } catch {
    if (version === ui.draftVersion && request === openingRequest)
      ui.error = t(
        "pengolo.copy.this_conversation_is_no_longer_available_to_your",
      );
  } finally {
    if (request === openingRequest) opening.value = false;
  }
}
async function selectChat(id: string) {
  await ui.prepareStoredChat();
  if (window.matchMedia("(max-width: 800px)").matches) ui.sidebarOpen = false;
  if (props.compact) await openChat(id);
  else await router.push(`/pengolo/${id}`);
}
async function newChat(projectId: string | null = null) {
  await ui.closeTemporary();
  chats.active = null;
  ui.draft(projectId);
  pending.value = null;
  if (projectId) {
    ui.temporary = false;
    ui.withLibrary = false;
  }
  if (!props.compact)
    await router.push(
      projectId
        ? { path: "/pengolo", query: { project: projectId } }
        : "/pengolo",
    );
}
async function startDocumentChat(files: FileRecord[]) {
  const scope = libraryScope.value || null;
  await newChat(scope);
  ui.temporary = false;
  ui.withLibrary = false;
  ui.files = files;
}
async function changeMode(selected: "normal" | "temporary" | "with-library") {
  await newChat();
  ui.temporary = selected !== "normal";
  ui.withLibrary = selected === "with-library";
}
async function answerQuestion(
  response: import("~/utils/pengolo-api").QuestionResponse,
) {
  if (!activeRun.value) throw new Error("Question unavailable");
  await runs.answer(activeRun.value.id, response);
}
async function send() {
  if (busy.value || !ui.prompt.trim() || !models.selected) return;
  ui.error = "";
  try {
    const input = {
      chatId: chats.active?.id ?? (ui.temporary ? ui.temporarySession : null),
      projectId: ui.temporary ? null : ui.projectId,
      prompt: ui.prompt,
      modelAlias: models.selected,
      agentIds: usePengoloAgents().selected,
      fileIds: ui.files.map((file) => file.id),
      graphContext: ui.graphContext,
      webSearch: ui.webEnabled,
      temporary: ui.temporary,
      withLibrary: ui.withLibrary,
    };
    const serialized = JSON.stringify(input);
    if (!pending.value || pending.value.serialized !== serialized)
      pending.value = { serialized, id: crypto.randomUUID() };
    const result = await runs.send({
      idempotencyKey: pending.value.id,
      ...input,
    });
    if (!result) return;
    ui.prompt = "";
    ui.files = [];
    pending.value = null;
    followsBottom.value = true;
    if (ui.temporary) ui.temporarySession = result.chat.id;
    else if (!props.compact) await router.replace(`/pengolo/${result.chat.id}`);
    await bottom();
  } catch (error: unknown) {
    ui.error =
      error instanceof Error &&
      error.message.startsWith(t("pengolo.copy.enter_a_public"))
        ? error.message
        : t("pengolo.copy.your_message_could_not_be_sent_retry_will");
  }
}
async function stop() {
  if (!activeRun.value) return;
  try {
    await runs.stop(activeRun.value.id);
  } catch {
    ui.error = t(
      "pengolo.copy.unable_to_confirm_cancellation_please_retry_stop",
    );
  }
}
async function bottom() {
  await nextTick();
  if (scroller.value && followsBottom.value)
    scroller.value.scrollTop = scroller.value.scrollHeight;
}
async function scroll() {
  if (scroller.value) {
    followsBottom.value =
      scroller.value.scrollHeight -
        scroller.value.scrollTop -
        scroller.value.clientHeight <
      80;
    if (
      scroller.value.scrollTop < 80 &&
      !followsBottom.value &&
      !opening.value &&
      chats.active?.data.messagesBefore &&
      !chats.loadingOlder
    ) {
      const element = scroller.value;
      const height = element.scrollHeight;
      const top = element.scrollTop;
      const chatId = chats.active.id;
      try {
        await chats.older(ui.temporary);
        await nextTick();
        if (chats.active?.id === chatId)
          element.scrollTop = top + element.scrollHeight - height;
      } catch {
        ui.error = t(
          "pengolo.copy.earlier_messages_could_not_be_loaded_scroll_up",
        );
      }
    }
  }
}
watch([streamingText, () => messages.value.length], () => void bottom());
let focusedMessage = "";
watch(
  [
    () => route.hash,
    () => messages.value.length,
    opening,
    () => chats.active?.id,
  ],
  async ([hash]) => {
    if (
      typeof hash !== "string" ||
      !/^#p-message-[A-Za-z0-9_-]{1,128}$/.test(hash)
    ) {
      focusedMessage = "";
      return;
    }
    const key = `${chats.active?.id}:${hash}`;
    if (focusedMessage === key) return;
    await nextTick();
    if (`${chats.active?.id}:${route.hash}` !== key) return;
    const target = document.getElementById(hash.slice(1));
    if (target && scroller.value?.contains(target)) {
      followsBottom.value = false;
      focusedMessage = key;
      target.scrollIntoView({ block: "center" });
      target.focus({ preventScroll: true });
    }
  },
  { immediate: true, flush: "post" },
);
watch(
  () => `${account.value?.tenant}/${account.value?.subject}`,
  () => {
    if (props.chatId && account.value) void openChat(props.chatId);
  },
);
watch(
  () => props.chatId,
  (id) => {
    if (id) {
      if (chats.active?.id !== id) void openChat(id);
    } else if (!props.compact && !ui.temporary) chats.active = null;
  },
  { immediate: true },
);
watch(
  () => route.query.project,
  (id) => {
    if (typeof id === "string" && !props.chatId) ui.projectId = id;
  },
  { immediate: true },
);
watch(
  () => route.query.project,
  (id) => {
    libraryScope.value = typeof id === "string" ? id : "";
  },
  { immediate: true },
);
let media: MediaQueryList | null = null;
function mobile(event: MediaQueryListEvent) {
  if (event.matches) ui.sidebarOpen = false;
}
onActivated(() => {
  if (props.chatId && chats.active?.id !== props.chatId)
    void openChat(props.chatId);
});
onMounted(() => {
  void initialize();
  media = window.matchMedia("(max-width: 800px)");
  if (media.matches) ui.sidebarOpen = false;
  media.addEventListener("change", mobile);
});
onUnmounted(() => {
  media?.removeEventListener("change", mobile);
});
</script>

<template>
  <div :class="['p-workspace', { compact }]">
    <UiBackdrop
      v-if="ui.sidebarOpen && !compact"
      class="p-sidebar-overlay"
      :label="$t('pengolo.copy.close_pengolo_sidebar')"
      @click="ui.sidebarOpen = false"
    />
    <aside
      v-if="!compact"
      :class="['p-sidebar', { closed: !ui.sidebarOpen }]"
      :inert="!ui.sidebarOpen"
      :aria-label="$t('pengolo.copy.pengolo_navigation')"
    >
      <div class="p-sidebar-inner">
        <div class="p-brand">
          <strong>{{ $t("chat_component.drawer.brand") }}</strong
          ><UiIconButton
            class="ms-auto"
            variant="ghost"
            type="button"
            @click="ui.sidebarOpen = false"
            :icon="RectangleGroupIcon"
            :label="$t('pengolo.copy.collapse_pengolo_sidebar')"
          />
        </div>
        <nav :aria-label="$t('pengolo.copy.pengolo_workspace')">
          <UiButton
            variant="ghost"
            type="button"
            @click="newChat()"
            align="start"
            class="w-full new"
          >
            <PencilSquareIcon />{{ $t("pengolo.copy.new_chat") }}</UiButton
          ><UiNavigationLink :active="view === 'scheduled'"
            ><NuxtLink to="/pengolo/scheduled"
              ><ClockIcon /><span>{{
                $t("pengolo.copy.scheduled")
              }}</span></NuxtLink
            ></UiNavigationLink
          ><UiNavigationLink :active="view === 'library'"
            ><NuxtLink to="/pengolo/library"
              ><BookOpenIcon /><span>{{
                $t("pengolo.copy.library")
              }}</span></NuxtLink
            ></UiNavigationLink
          >
        </nav>
        <div class="p-sidebar-history">
          <div class="p-section-heading p-project-heading">
            <UiButton
              variant="ghost"
              type="button"
              :aria-expanded="projectsExpanded"
              @click="projectsExpanded = !projectsExpanded"
            >
              <ChevronDownIcon :class="{ rotated: !projectsExpanded }" />{{
                $t("pengolo.copy.projects")
              }}</UiButton
            ><UiIconButton
              class="p-create-project"
              variant="ghost"
              type="button"
              @click="
                editingProject = null;
                creatingProject = true;
              "
              :icon="PlusIcon"
              :label="$t('pengolo.copy.create_project')"
            />
          </div>
          <Transition name="p-pop"
            ><div v-if="projectsExpanded" class="p-projects">
              <AiProjectRow
                v-for="item in projects.items"
                :key="item.id"
                :project="item"
                :editable="item.ownerId === account?.subject"
                @toggle="toggleProject($event, item.id)"
                @edit="
                  editingProject = item;
                  creatingProject = true;
                "
              >
                <div class="p-project-children">
                  <div class="p-project-actions">
                    <UiIconButton
                      variant="ghost"
                      type="button"
                      @click="newChat(item.id)"
                      :icon="PlusIcon"
                      :label="
                        $t('pengolo.copy.new_chat_in_value0', {
                          value0: item.data.name,
                        })
                      "
                    /><UiNavigationLink
                      ><NuxtLink
                        :to="{
                          path: '/pengolo/library',
                          query: { project: item.id },
                        }"
                        :aria-label="
                          $t('pengolo.copy.library_for_value0', {
                            value0: item.data.name,
                          })
                        "
                        :title="
                          $t('pengolo.copy.library_for_value0', {
                            value0: item.data.name,
                          })
                        "
                        ><BookOpenIcon /></NuxtLink
                    ></UiNavigationLink>
                  </div>
                  <AiChatRow
                    v-for="chat in chats.items.filter(
                      (chat) => chat.projectId === item.id,
                    )"
                    :key="chat.id"
                    :chat="chat"
                    @select="selectChat"
                  /><UiButton
                    variant="ghost"
                    v-if="chats.cursors[item.id]"
                    type="button"
                    @click="chats.load(true, item.id)"
                  >
                    {{ $t("pengolo.copy.load_more") }}
                  </UiButton>
                  <p
                    v-if="
                      !chats.items.some((chat) => chat.projectId === item.id)
                    "
                    class="p-muted"
                  >
                    {{ $t("pengolo.copy.no_recent_conversations") }}
                  </p>
                </div>
              </AiProjectRow>
              <p v-if="!projects.items.length" class="p-muted p-sidebar-empty">
                {{ $t("pengolo.copy.a_place_for_your_focused_work") }}
              </p>
            </div></Transition
          >
          <div class="p-section-heading">
            <UiButton
              variant="ghost"
              type="button"
              :aria-expanded="chatsExpanded"
              @click="chatsExpanded = !chatsExpanded"
            >
              <ChevronDownIcon :class="{ rotated: !chatsExpanded }" />{{
                $t("pengolo.copy.chats")
              }}
            </UiButton>
          </div>
          <Transition name="p-pop"
            ><div v-if="chatsExpanded">
              <div
                v-if="chats.loading"
                class="p-skeleton"
                :aria-label="$t('pengolo.copy.loading_conversations')"
              />
              <AiChatRow
                v-for="chat in chats.items.filter((chat) => !chat.projectId)"
                :key="chat.id"
                :chat="chat"
                @select="selectChat"
              />
              <p
                v-if="!chats.items.length && !chats.loading"
                class="p-muted p-sidebar-empty"
              >
                {{ $t("pengolo.copy.your_conversations_will_appear_here") }}
              </p>
              <UiButton
                variant="ghost"
                v-if="chats.after"
                type="button"
                :disabled="chats.loading"
                @click="chats.load(true)"
              >
                {{ $t("pengolo.copy.load_more_conversations") }}
              </UiButton>
            </div></Transition
          >
        </div>
        <div class="p-sidebar-footer">
          <span class="p-running-dot" />{{
            $t("pengolo.copy.connected_to_your_galadril_workspace")
          }}
        </div>
      </div>
    </aside>
    <main
      :class="[
        'p-main',
        { empty: (!view || view === 'chat') && !messages.length && !opening },
      ]"
    >
      <header class="p-header">
        <div>
          <UiIconButton
            variant="ghost"
            v-if="!ui.sidebarOpen && !compact"
            type="button"
            @click="ui.sidebarOpen = true"
            :icon="Bars3Icon"
            :label="$t('pengolo.copy.open_pengolo_sidebar')"
          /><span
            v-if="project"
            class="p-project-label"
            :style="{ color: project.data.color }"
            ><AiProjectIcon
              :name="project.data.icon"
              :color="project.data.color"
            />{{ project.data.name }}</span
          >
          <h1 v-if="view === 'library'">{{ $t("pengolo.copy.library") }}</h1>
          <h1 v-else-if="view === 'scheduled'">
            {{ $t("pengolo.copy.scheduled") }}
          </h1>
          <h1 v-else>
            {{ chats.active?.data.title ?? $t("chat_component.drawer.brand") }}
          </h1>
        </div>
        <div v-if="!view || view === 'chat'" class="p-chat-mode">
          <label v-if="ui.temporary" class="p-temporary-library">
            <UiCheckbox
              type="checkbox"
              :checked="ui.withLibrary"
              :disabled="busy"
              @change="
                changeMode(
                  ($event.target as HTMLInputElement).checked
                    ? 'with-library'
                    : 'temporary',
                )
              "
            />
            {{ $t("pengolo.copy.use_my_personal_library") }}
          </label>
          <UiButton
            variant="ghost"
            type="button"
            :aria-label="$t('pengolo.copy.temporary_chat')"
            :aria-pressed="ui.temporary"
            @click="changeMode(ui.temporary ? 'normal' : 'temporary')"
            :loading="busy"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              aria-hidden="true"
            >
              <path
                d="M8 3.8a8.8 8.8 0 0 1 3-.5m4 .5a8.8 8.8 0 0 1 2.6 1.5m2.6 3a8.8 8.8 0 0 1 .6 3m-.5 4a8.8 8.8 0 0 1-1.6 2.7m-3 2.5a8.8 8.8 0 0 1-3 .5m-4-.5-4.7 1.2 1.2-4.7m-1.5-3a8.8 8.8 0 0 1 0-3.1m1.2-3.7a8.8 8.8 0 0 1 2-2.3"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </UiButton>
        </div>
      </header>
      <div v-if="ui.error || models.error" class="p-notice" role="alert">
        <span>{{ ui.error || models.error }}</span
        ><UiIconButton
          variant="ghost"
          type="button"
          @click="
            ui.error = '';
            models.error = '';
          "
          :icon="XMarkIcon"
          :label="$t('pengolo.copy.dismiss_error')"
        />
      </div>
      <section v-if="view === 'scheduled'" class="p-scheduled">
        <div class="p-welcome-mark"><ClockIcon /></div>
        <h2>{{ $t("pengolo.copy.coming_soon") }}</h2>
        <p>
          {{
            $t("pengolo.copy.scheduled_reports_recurring_tasks_and_monitoring")
          }}<br />{{ $t("pengolo.copy.right_here_in_your_workspace") }}
        </p>
        <NuxtLink to="/pengolo" class="p-button">{{
          $t("pengolo.copy.start_a_conversation")
        }}</NuxtLink>
      </section>
      <section v-else-if="view === 'library'" class="p-library-page">
        <div class="p-page-title">
          <h2>{{ $t("pengolo.copy.your_knowledge_ready_to_use") }}</h2>
          <p>
            {{
              $t(
                "pengolo.copy.reuse_documents_across_conversations_without_uploading_them_again",
              )
            }}
          </p>
          <label
            >{{ $t("pengolo.copy.library")
            }}<UiSelect
              v-model="libraryScope"
              :aria-label="$t('pengolo.copy.choose_library')"
              :options="[
                { label: $t('pengolo.copy.personal_library'), value: '' },
                ...projects.items.flatMap((item) => [
                  {
                    label: String(item.data.name).trim(),
                    value: item.id,
                  },
                ]),
              ]"
          /></label>
        </div>
        <AiLibrary
          :project-id="libraryScope || null"
          @start-chat="startDocumentChat"
        />
      </section>
      <template v-else>
        <div v-if="ui.temporary" class="p-temporary-banner" role="status">
          {{ $t("pengolo.copy.temporary_chat_2") }}
          {{
            ui.withLibrary
              ? $t("pengolo.copy.your_personal_library_is_available")
              : $t("pengolo.copy.no_library_documents_are_reused")
          }}
          {{ $t("pengolo.copy.not_saved_in_history") }}
        </div>
        <div ref="scroller" class="p-conversation" @scroll.passive="scroll">
          <div v-if="opening" class="p-thread">
            <div class="p-skeleton" />
            <div class="p-skeleton" />
          </div>
          <div v-else-if="!messages.length" class="p-welcome">
            <h2>{{ $t("pengolo.copy.how_can_pengolo_help") }}</h2>
          </div>
          <div v-else class="p-thread">
            <p v-if="chats.loadingOlder" role="status" class="p-muted">
              {{ $t("pengolo.copy.loading_earlier_messages") }}
            </p>
            <AiMessage
              v-for="message in messages"
              :key="message.id"
              :message="message"
              :anchor-id="compact ? undefined : `p-message-${message.id}`"
            /><AiMessage
              v-if="activeRun"
              :message="{
                id: activeRun.id,
                role: 'assistant',
                text: streamingText,
              }"
              streaming
            />
            <AiRunActivity
              v-if="activityRun"
              :events="activityRun.events"
              :status="activityRun.status"
              :can-answer="activityRun.canAnswer"
              :submit="answerQuestion"
            />
            <p
              v-if="
                activeRun &&
                !activeRun.events.some((event) => event.kind === 'activity')
              "
              class="p-tool-status"
              role="status"
            >
              <span class="p-running-dot" />{{ toolStatus }}
            </p>
            <p v-if="activeRun?.error" class="p-muted" role="status">
              {{ activeRun.error }}
            </p>
          </div>
        </div>
        <div class="p-composer-dock">
          <UiButton
            variant="ghost"
            v-if="!followsBottom && activeRun"
            type="button"
            @click="
              followsBottom = true;
              bottom();
            "
          >
            {{ $t("pengolo.copy.latest_response") }}</UiButton
          ><AiInputArea :busy="busy" @submit="send" @stop="stop" />
          <p class="p-composer-footnote">
            {{
              $t(
                "pengolo.copy.grounded_in_your_authorized_galadril_data_verify_important",
              )
            }}
          </p>
        </div>
      </template>
    </main>
    <AiProjectEditor
      :open="creatingProject"
      :project="editingProject"
      @close="creatingProject = false"
    />
  </div>
</template>
