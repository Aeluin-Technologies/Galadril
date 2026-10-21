<script setup lang="ts">
import { toRefs, type UnwrapRef } from "vue";
import type { usePermissions } from "~/composables/usePermissions";
const props = defineProps<{
  controller: UnwrapRef<ReturnType<typeof usePermissions>>;
}>();
const {
  selectedNode,
  resourceLabel,
  relatedEdges,
  nodeLabel,
  manageSelectedPerson,
  editSelectedRules,
  editorOpen,
  busy,
  renameSelected,
} = toRefs(props.controller);
</script>
<template>
  <div class="space-y-5">
    <AdministrationAccessMap :controller="controller" />
    <UiButton variant="secondary" @click="editorOpen = true">{{
      $t("administration.manage_access")
    }}</UiButton>
    <UiDialog
      :close-label="$t('administration.close_editor')"
      :open="editorOpen"
      :title="
        selectedNode
          ? $t('administration.copy.access_for_value0', {
              value0: selectedNode.label,
            })
          : $t('administration.manage_access')
      "
      @close="editorOpen = false"
    >
      <div data-administration class="space-y-6">
        <AdministrationDisplayNameForm
          v-if="
            selectedNode && ['user', 'role'].includes(selectedNode.data.kind)
          "
          :current-name="selectedNode.label"
          :busy="busy"
          @save="renameSelected"
        />
        <div
          v-if="selectedNode"
          class="mt-4 rounded-xl bg-primary-subtle/60 p-4"
        >
          <h3>{{ selectedNode.label }}</h3>
          <p class="hint">
            {{ resourceLabel(selectedNode.data.kind) }}
          </p>
          <ul class="mt-2 space-y-2 text-sm text-foreground">
            <li v-for="edge in relatedEdges" :key="edge.id">
              {{ nodeLabel(edge.source) }} → {{ nodeLabel(edge.target) }}
              <span class="text-muted">· {{ edge.label }}</span>
            </li>
          </ul>
          <p v-if="!relatedEdges.length" class="hint">
            {{
              $t(
                "administration.copy.no_direct_relationships_are_shown_in_this_view",
              )
            }}
          </p>
          <div class="mt-4 flex flex-wrap gap-3">
            <UiButton
              v-if="selectedNode.data.kind === 'user'"
              variant="secondary"
              @click="manageSelectedPerson"
              >{{ $t("administration.copy.manage_this_person") }}</UiButton
            >
            <UiButton variant="secondary" @click="editSelectedRules">{{
              $t("administration.copy.edit_general_access_rules")
            }}</UiButton>
          </div>
          <p class="hint">
            {{
              $t(
                "administration.copy.assignments_and_resource_permissions_below_are_managed_through",
              )
            }}
          </p>
          <p v-if="selectedNode.data.kind === 'role'" class="hint">
            {{
              $t(
                "administration.copy.role_names_are_identifiers_used_by_sign_in",
              )
            }}
          </p>
        </div>
        <AdministrationAccessForm :controller="controller" />
      </div>
      <template #footer
        ><UiButton variant="secondary" @click="editorOpen = false">{{
          $t("actions.close")
        }}</UiButton></template
      >
    </UiDialog>
  </div>
</template>
