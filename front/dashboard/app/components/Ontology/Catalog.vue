<script setup lang="ts">
import type {
  SemanticPackage,
  TenantOntology,
  OntologyResource,
} from "~/types/ontology";
const search = defineModel<string>("search", { required: true });
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
    class="border-r border-border bg-surface p-4"
    :aria-label="$t('ontologyManager.ontology_catalog')"
  >
    <UiInput
      v-model="search"
      :aria-label="$t('ontologyManager.search_ontology_types')"
      :placeholder="$t('ontologyManager.search_types')"
    />
    <h2 class="mt-5 text-xs font-semibold uppercase text-muted">
      {{ $t("ontologyManager.organization_ontologies") }}
    </h2>
    <UiButton
      variant="ghost"
      align="start"
      v-for="item in ontologies"
      :key="item.ontologyId"
      class="mt-1 w-full"
      :aria-pressed="id === item.ontologyId"
      :disabled="busy || dirty"
      @click="emit('open', item)"
    >
      {{ item.displayName }}
    </UiButton>
    <h2 class="mt-5 text-xs font-semibold uppercase text-muted">
      {{ $t("ontologyManager.plugin_originals") }}
    </h2>
    <UiButton
      variant="ghost"
      align="start"
      v-for="p in packages"
      :key="p.id"
      class="mt-1 w-full"
      :disabled="busy || dirty"
      @click="emit('start', p)"
    >
      {{ p.name
      }}<span class="block text-xs text-muted"
        >{{ p.plugin_id }} · {{ p.version }}</span
      >
    </UiButton>
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
      {{ r.display_name }}
    </UiButton>
  </aside>
</template>
