<script setup lang="ts">
import { toRefs, type UnwrapRef } from "vue";
import type { usePermissions } from "~/composables/usePermissions";
import { ArrowPathIcon } from "@heroicons/vue/24/outline";
import { VueFlow, Position } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { Controls } from "@vue-flow/controls";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import "@vue-flow/controls/dist/style.css";
type Controller = Pick<
  UnwrapRef<ReturnType<typeof usePermissions>>,
  | "busy"
  | "perform"
  | "loadGraph"
  | "peopleAfter"
  | "moreDirectory"
  | "rolesAfter"
  | "search"
  | "graph"
  | "selectAccessNode"
  | "selectAccessEdge"
>;
const props = defineProps<{ controller: Controller }>();
const {
  busy,
  perform,
  loadGraph,
  peopleAfter,
  moreDirectory,
  rolesAfter,
  search,
  graph,
  selectAccessNode,
  selectAccessEdge,
} = toRefs(props.controller);
const flowNodes = computed(() =>
  graph.value.nodes.map((node) => ({
    ...node,
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
  })),
);
function selectWithKeyboard(event: KeyboardEvent) {
  if (
    (event.key !== "Enter" && event.key !== " ") ||
    !(event.target instanceof Element)
  )
    return;
  const node = event.target.closest(".vue-flow__node");
  const edge = event.target.closest(".vue-flow__edge");
  const id = (node ?? edge)?.getAttribute("data-id");
  if (!id) return;
  event.preventDefault();
  if (node) selectAccessNode.value(id);
  else selectAccessEdge.value(id);
}
</script>
<template>
  <UiCard class="">
    <div class="flex items-start justify-between gap-3">
      <div>
        <h2>{{ $t("administration.copy.see_who_can_reach_your_data") }}</h2>
        <p>
          {{
            $t(
              "administration.copy.select_a_person_role_or_resource_to_inspect",
            )
          }}
        </p>
      </div>
      <UiIconButton
        type="button"
        variant="secondary"
        :aria-busy="busy"
        @click="
          perform(
            loadGraph,
            $t('administration.copy.access_map_updated'),
            $t('administration.copy.the_access_map_could_not_be_loaded_try'),
          )
        "
        :icon="ArrowPathIcon"
        :label="$t('administration.copy.reload_access_map')"
        :loading="busy"
      />
    </div>
    <UiButton
      v-if="peopleAfter"
      variant="secondary"
      @click="
        perform(
          () => moreDirectory('users'),
          '',
          $t('administration.copy.more_people_could_not_be_loaded'),
        )
      "
      :loading="busy"
      >{{ $t("administration.copy.load_more_people") }}</UiButton
    >
    <UiButton
      v-if="rolesAfter"
      variant="secondary"
      @click="
        perform(
          () => moreDirectory('roles'),
          '',
          $t('administration.copy.more_roles_could_not_be_loaded'),
        )
      "
      :loading="busy"
      >{{ $t("administration.copy.load_more_roles") }}</UiButton
    >
    <label class="sr-only" for="access-search">{{
      $t("administration.copy.find_people_roles_or_resources")
    }}</label
    ><UiInput
      id="access-search"
      v-model="search"
      type="search"
      :placeholder="$t('administration.copy.find_a_person_role_or_document')"
      maxlength="128"
      class="mt-5"
    />
    <div class="mt-4 flex flex-wrap gap-4 text-xs text-muted">
      <span>{{ $t("administration.copy.people_2") }}</span
      ><span class="text-primary-foreground">{{
        $t("administration.copy.roles_2")
      }}</span
      ><span>{{ $t("administration.copy.data_spaces") }}</span
      ><span>{{ $t("administration.copy.resources") }}</span>
    </div>
    <div
      class="mt-4 h-80 overflow-hidden rounded-xl border border-border bg-canvas"
      role="region"
      :aria-label="$t('administration.copy.access_map')"
      @keydown="selectWithKeyboard"
    >
      <VueFlow
        v-if="graph.nodes.length"
        :nodes="flowNodes"
        :edges="graph.edges"
        :nodes-draggable="false"
        :nodes-connectable="false"
        fit-view-on-init
        :min-zoom="0.25"
        :max-zoom="1.5"
        @node-click="({ node }) => selectAccessNode(node.id)"
        @edge-click="({ edge }) => selectAccessEdge(edge.id)"
        ><Background pattern-color="#e7e5e4" :gap="20" /><Controls
          :show-interactive="false"
      /></VueFlow>
      <p v-else class="p-8 text-center">
        {{
          busy
            ? $t("administration.copy.loading_access")
            : $t(
                "administration.copy.no_visible_access_connections_match_your_search",
              )
        }}
      </p>
    </div>
    <p v-if="graph.truncated" class="hint">
      {{
        $t("administration.copy.this_map_shows_a_limited_set_of_connections")
      }}
    </p>
  </UiCard>
</template>
