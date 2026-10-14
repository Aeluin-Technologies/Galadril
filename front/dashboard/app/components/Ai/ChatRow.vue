<script setup lang="ts">
import { EllipsisHorizontalIcon, TrashIcon } from "@heroicons/vue/24/outline";
import { onClickOutside } from "@vueuse/core";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloUi } from "~/stores/usePengoloUi";
import type { ChatRecord } from "~/utils/pengolo-api";
const props = defineProps<{ chat: ChatRecord }>();
const emit = defineEmits<{ select: [id: string] }>();
const chats = usePengoloChats();
const projects = usePengoloProjects();
const ui = usePengoloUi();
const menu = ref(false);
const container = ref<HTMLElement | null>(null);
const renaming = ref(false);
const deleting = ref(false);
const name = ref("");
const busy = ref(false);
onClickOutside(container, () => {
  menu.value = false;
});
async function update(changes: Record<string, unknown>) {
  busy.value = true;
  try {
    await chats.update(props.chat, changes);
    menu.value = false;
    renaming.value = false;
  } catch {
    ui.error =
      "The conversation could not be updated. Its permissions or revision may have changed.";
  } finally {
    busy.value = false;
  }
}
async function remove() {
  busy.value = true;
  try {
    await chats.remove(props.chat);
    deleting.value = false;
    if (!chats.active) await navigateTo("/pengolo");
  } catch {
    ui.error =
      "Stop any active generation, then try deleting this conversation again.";
  } finally {
    busy.value = false;
  }
}
function move(event: Event) {
  const project = (event.target as HTMLSelectElement).value;
  void update({ projectId: project || null, moveProject: true });
}
</script>

<template>
  <div
    ref="container"
    :class="['p-chat-row', { active: chats.active?.id === chat.id }]"
  >
    <UtilsButton
      variant="ghost"
      type="button"
      class="p-chat-link"
      :aria-current="chats.active?.id === chat.id ? 'page' : undefined"
      @click="emit('select', chat.id)"
    >
      <span v-if="chat.data.pinned" aria-label="Pinned">⌖</span
      ><span>{{ chat.data.title }}</span
      ><i
        v-if="chat.data.activeGeneration"
        class="p-running-dot"
        aria-label="Generation in progress"
      />
    </UtilsButton>
    <UtilsButton
      variant="ghost"
      type="button"
      class="p-icon p-row-action"
      :aria-label="`Options for ${chat.data.title}`"
      :aria-expanded="menu"
      @click="menu = !menu"
    >
      <EllipsisHorizontalIcon />
    </UtilsButton>
    <UtilsButton
      variant="ghost"
      type="button"
      class="p-icon p-row-action danger"
      :aria-label="`Delete ${chat.data.title}`"
      @click="deleting = true"
    >
      <TrashIcon />
    </UtilsButton>
    <Transition name="p-pop"
      ><div
        v-if="menu"
        class="p-menu p-chat-menu"
        role="menu"
        @keydown.esc="menu = false"
      >
        <UtilsButton
          variant="ghost"
          type="button"
          role="menuitem"
          @click="
            name = chat.data.title;
            renaming = true;
            menu = false;
          "
        >
          Rename</UtilsButton
        ><UtilsButton
          variant="ghost"
          type="button"
          role="menuitem"
          :disabled="busy"
          @click="update({ pinned: !chat.data.pinned })"
        >
          {{ chat.data.pinned ? "Unpin" : "Pin" }}</UtilsButton
        ><label
          >Move to project<select
            :value="chat.projectId ?? ''"
            :disabled="busy"
            aria-label="Move conversation to project"
            @change="move"
          >
            <option value="">Outside projects</option>
            <option
              v-for="project in projects.items"
              :key="project.id"
              :value="project.id"
            >
              {{ project.data.icon }} {{ project.data.name }}
            </option>
          </select></label
        ><UtilsButton
          variant="ghost"
          v-if="chat.projectId"
          type="button"
          role="menuitem"
          @click="update({ projectId: null, moveProject: true })"
        >
          Remove from project
        </UtilsButton>
      </div></Transition
    >
    <AiDialog
      :open="renaming"
      title="Rename conversation"
      @close="renaming = false"
      ><form
        id="rename-chat"
        class="p-form"
        @submit.prevent="update({ title: name })"
      >
        <label
          >Conversation name<input
            v-model="name"
            autofocus
            required
            maxlength="256"
        /></label>
      </form>
      <template #footer
        ><UtilsButton
          variant="secondary"
          type="button"
          class="p-button"
          @click="renaming = false"
        >
          Cancel</UtilsButton
        ><UtilsButton
          variant="primary"
          type="submit"
          form="rename-chat"
          class="p-button primary"
          :disabled="busy"
        >
          Save name
        </UtilsButton></template
      ></AiDialog
    >
    <AiDialog
      :open="deleting"
      title="Delete this conversation?"
      @close="deleting = false"
      ><p>
        {{ chat.data.title }} will be removed. Files kept in a library remain
        available.
      </p>
      <template #footer
        ><UtilsButton
          variant="secondary"
          type="button"
          class="p-button"
          @click="deleting = false"
        >
          Keep conversation</UtilsButton
        ><UtilsButton
          variant="danger"
          type="button"
          class="p-button danger"
          :disabled="busy"
          @click="remove"
        >
          Delete conversation
        </UtilsButton></template
      ></AiDialog
    >
  </div>
</template>
