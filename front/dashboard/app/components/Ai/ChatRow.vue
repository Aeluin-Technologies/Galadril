<script setup lang="ts">
const { t } = useI18n();

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
    ui.error = t(
      "pengolo.copy.the_conversation_could_not_be_updated_its_permissions",
    );
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
    ui.error = t(
      "pengolo.copy.stop_any_active_generation_then_try_deleting_this",
    );
  } finally {
    busy.value = false;
  }
}
function move(project: string | undefined) {
  void update({ projectId: project || null, moveProject: true });
}
</script>

<template>
  <div
    ref="container"
    :class="['p-chat-row', { active: chats.active?.id === chat.id }]"
  >
    <UiButton
      variant="ghost"
      type="button"
      :aria-current="chats.active?.id === chat.id ? 'page' : undefined"
      @click="emit('select', chat.id)"
      align="start"
      class="min-w-0 flex-1"
    >
      <span v-if="chat.data.pinned" :aria-label="$t('pengolo.copy.pinned')"
        >⌖</span
      ><span class="truncate">{{ chat.data.title }}</span
      ><i
        v-if="chat.data.activeGeneration"
        class="p-running-dot"
        :aria-label="$t('pengolo.copy.generation_in_progress')"
      />
    </UiButton>
    <UiIconButton
      class="p-row-action"
      variant="ghost"
      type="button"
      :aria-expanded="menu"
      @click="menu = !menu"
      :icon="EllipsisHorizontalIcon"
      :label="
        $t('pengolo.copy.options_for_value0', { value0: chat.data.title })
      "
    />
    <UiIconButton
      class="p-row-action"
      variant="ghost"
      type="button"
      @click="deleting = true"
      :icon="TrashIcon"
      :label="$t('pengolo.copy.delete_value0', { value0: chat.data.title })"
    />
    <Transition name="p-pop"
      ><div
        v-if="menu"
        class="p-menu p-chat-menu"
        role="menu"
        @keydown.esc="menu = false"
      >
        <UiButton
          variant="ghost"
          type="button"
          role="menuitem"
          align="start"
          class="w-full"
          @click="
            name = chat.data.title;
            renaming = true;
            menu = false;
          "
        >
          {{ $t("pengolo.copy.rename") }}</UiButton
        ><UiButton
          variant="ghost"
          type="button"
          role="menuitem"
          align="start"
          class="w-full"
          @click="update({ pinned: !chat.data.pinned })"
          :loading="busy"
        >
          {{
            chat.data.pinned ? $t("pengolo.copy.unpin") : $t("pengolo.copy.pin")
          }}</UiButton
        ><label
          >{{ $t("pengolo.copy.move_to_project")
          }}<UiSelect
            :disabled="busy"
            :aria-label="$t('pengolo.copy.move_conversation_to_project')"
            :options="[
              { label: $t('pengolo.copy.outside_projects').trim(), value: '' },
              ...projects.items.flatMap((project) => [
                {
                  label: (
                    String(project.data.icon) +
                    ' ' +
                    String(project.data.name)
                  ).trim(),
                  value: project.id,
                },
              ]),
            ]"
            @update:model-value="move"
            :model-value="chat.projectId ?? ''" /></label
        ><UiButton
          variant="ghost"
          v-if="chat.projectId"
          type="button"
          role="menuitem"
          align="start"
          class="w-full"
          @click="update({ projectId: null, moveProject: true })"
        >
          {{ $t("pengolo.copy.remove_from_project") }}
        </UiButton>
      </div></Transition
    >
    <UiDialog
      :open="renaming"
      :title="$t('pengolo.copy.rename_conversation')"
      @close="renaming = false"
      ><form
        id="rename-chat"
        class="p-form"
        @submit.prevent="update({ title: name })"
      >
        <label
          >{{ $t("pengolo.copy.conversation_name")
          }}<UiInput v-model="name" autofocus required maxlength="256"
        /></label>
      </form>
      <template #footer
        ><UiButton variant="secondary" type="button" @click="renaming = false">
          {{ $t("builder.config_modal.buttons.cancel") }}</UiButton
        ><UiButton
          variant="primary"
          type="submit"
          form="rename-chat"
          :loading="busy"
        >
          {{ $t("pengolo.copy.save_name") }}
        </UiButton></template
      ></UiDialog
    >
    <UiDialog
      :open="deleting"
      :title="$t('pengolo.copy.delete_this_conversation')"
      @close="deleting = false"
      ><p>
        {{ chat.data.title }}
        {{ $t("pengolo.copy.will_be_removed_files_kept_in_a_library") }}
      </p>
      <template #footer
        ><UiButton variant="secondary" type="button" @click="deleting = false">
          {{ $t("pengolo.copy.keep_conversation") }}</UiButton
        ><UiButton
          variant="danger"
          type="button"
          @click="remove"
          :loading="busy"
        >
          {{ $t("pengolo.copy.delete_conversation") }}
        </UiButton></template
      ></UiDialog
    >
  </div>
</template>
