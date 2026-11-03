<script setup lang="ts">
import {
  CubeIcon,
  Squares2X2Icon,
  ListBulletIcon,
  LinkIcon,
} from "@heroicons/vue/24/outline";
import type {
  SemanticPackage,
  TenantOntology,
  OntologyResource,
} from "~/types/ontology";
const search = defineModel<string>("search", { required: true });
const view = defineModel<string>("view", { required: true });
defineProps<{
  packages: SemanticPackage[];
  ontologies: TenantOntology[];
  types: OntologyResource[];
  id: string;
  selected: string;
  busy: boolean;
  dirty: boolean;
  document?: unknown;
}>();
const emit = defineEmits<{
  open: [item: TenantOntology];
  start: [item: SemanticPackage];
  select: [id: string];
}>();
</script>
<template>
  <aside
    class="min-h-0 overflow-y-auto border-r border-border bg-surface p-4"
    :aria-label="$t('ontologyManager.ontology_catalog')"
  >
    <UiInput
      v-model="search"
      :aria-label="$t('ontologyManager.search_ontology_types')"
      :placeholder="$t('ontologyManager.search_types')"
    />
    <nav
      v-if="document"
      class="mt-4 space-y-1 border-b border-border pb-4"
      :aria-label="$t('ontologyManager.resource_views')"
    >
      <UiButton
        v-for="(icon, name) in {
          overview: Squares2X2Icon,
          properties: ListBulletIcon,
          relationships: LinkIcon,
        }"
        :key="name"
        variant="ghost"
        align="start"
        class="w-full"
        :aria-pressed="view === name"
        @click="view = name"
        ><component :is="icon" aria-hidden="true" class="mr-2 h-4 w-4" />{{
          $t(`ontologyManager.views.${name}`)
        }}</UiButton
      >
    </nav>
    <h2 v-if="document" class="mt-5 text-xs font-semibold uppercase text-muted">
      {{ $t("ontologyManager.object_types") }}
    </h2>
    <UiButton
      variant="ghost"
      align="start"
      v-for="r in types.filter((r) =>
        r.display_name.toLowerCase().includes(search.toLowerCase()),
      )"
      :key="r.resource_id"
      class="mt-1 w-full"
      :aria-pressed="selected === r.resource_id"
      @click="emit('select', r.resource_id)"
    >
      <CubeIcon aria-hidden="true" class="mr-2 h-4 w-4 shrink-0" />{{
        r.display_name
      }}
    </UiButton>
  </aside>
</template>
