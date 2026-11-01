<script setup lang="ts">
const {
  canManage,
  canCreate,
  canPublish,
  packages,
  ontologies,
  selectedPackage,
  document,
  id,
  name,
  revision,
  targetId,
  targetRevision,
  mappings,
  selected,
  search,
  tab,
  busy,
  error,
  notice,
  reviewing,
  pending,
  pipelines,
  pipelineId,
  blockId,
  pipelineBlocks,
  dirty,
  types,
  properties,
  resource,
  links,
  slots,
  targetTypes,
  graphNodes,
  graphEdges,
  newTypeName,
  newTypeId,
  newPropertyName,
  newPropertyId,
  newPropertyType,
  newOntology,
  discard,
  review,
  publish,
  open,
  start,
  chooseTarget,
  setMapping,
  choosePipeline,
  bind,
  save,
  addType,
  addProperty,
} = useOntologyWorkspace();
</script>
<template>
  <section
    class="flex min-h-[calc(100vh-3.5rem)] flex-col bg-canvas text-foreground"
    :aria-label="$t('ontologyManager.ontology_manager')"
  >
    <header
      class="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-5 py-3"
    >
      <div>
        <NuxtLink
          to="/pipeline"
          class="text-xs text-muted hover:text-primary-foreground"
          >{{ $t("ontologyManager.all_pipelines") }}</NuxtLink
        >
        <h1 class="text-lg font-semibold">
          {{ $t("ontologyManager.ontology_manager") }}
        </h1>
      </div>
      <div class="flex items-center gap-2">
        <UiButton
          variant="secondary"
          :disabled="busy || !dirty"
          @click="discard"
          >{{ $t("ontologyManager.discard_changes") }}</UiButton
        >
        <UiButton
          variant="secondary"
          :disabled="busy || dirty || !canCreate"
          @click="newOntology"
          >{{ $t("ontologyManager.new_ontology") }}</UiButton
        ><UiButton
          variant="secondary"
          :disabled="
            busy ||
            !canManage ||
            !document ||
            !name.trim() ||
            (!!document.plugin_origin && !selectedPackage)
          "
          @click="review"
          >{{ $t("ontologyManager.review_changes") }}</UiButton
        ><UiButton
          :disabled="busy || dirty || !revision || !canPublish"
          @click="publish"
          >{{ $t("ontologyManager.publish_ontology") }}</UiButton
        >
      </div>
    </header>
    <p
      v-if="!busy && !canManage"
      role="status"
      class="border-b border-border px-5 py-2 text-sm text-muted"
    >
      {{ $t("ontologyManager.readOnly") }}
    </p>
    <p
      v-if="busy && !document"
      role="status"
      class="px-5 py-2 text-sm text-muted"
    >
      {{ $t("ontologyManager.loading") }}
    </p>
    <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
    <p
      v-if="notice"
      role="status"
      class="border-b border-border bg-surface px-5 py-2 text-sm text-muted"
    >
      {{ notice }}
    </p>
    <div class="grid flex-1 lg:grid-cols-[250px_minmax(0,1fr)]">
      <OntologyCatalog
        v-model:search="search"
        :packages="packages"
        :ontologies="ontologies"
        :types="types"
        :id="id"
        :selected="selected"
        :busy="busy"
        :dirty="dirty"
        :document="document"
        @open="open"
        @start="start"
        @select="(value) => (selected = value)"
      />
      <main v-if="document" class="min-w-0 space-y-4 p-4 sm:p-6">
        <div class="grid gap-3 sm:grid-cols-2">
          <UiField :label="$t('ontologyManager.ontology_name')" v-slot="field"
            ><UiInput
              v-model="name"
              :id="field.id"
              :disabled="busy || !canManage"
              :aria-label="$t('ontologyManager.ontology_name')" /></UiField
          ><UiField
            :label="$t('ontologyManager.ontology_identifier')"
            v-slot="field"
            ><UiInput
              v-model="id"
              :id="field.id"
              :aria-label="$t('ontologyManager.ontology_identifier')"
              :disabled="!!revision || busy || !canManage"
          /></UiField>
        </div>
        <div
          v-if="canManage && !selectedPackage && !document.plugin_origin"
          class="flex flex-wrap items-end gap-2 rounded-control border border-border bg-surface p-3"
        >
          <UiField
            :label="$t('ontologyManager.object_type_name')"
            v-slot="field"
            ><UiInput
              v-model="newTypeName"
              :id="field.id"
              :disabled="busy"
              :aria-label="$t('ontologyManager.object_type_name')"
              :placeholder="$t('ontologyManager.object_type_name')" /></UiField
          ><UiField
            :label="$t('ontologyManager.object_type_identifier')"
            v-slot="field"
            ><UiInput
              v-model="newTypeId"
              :id="field.id"
              :disabled="busy"
              :aria-label="$t('ontologyManager.object_type_identifier')"
              :placeholder="
                $t('ontologyManager.organization_person')
              " /></UiField
          ><UiButton
            variant="secondary"
            :disabled="busy || !newTypeName.trim()"
            @click="addType"
            >{{ $t("ontologyManager.add_object_type") }}</UiButton
          >
        </div>
        <div
          class="flex gap-1 border-b border-border"
          :aria-label="$t('ontologyManager.resource_views')"
        >
          <UiButton
            variant="ghost"
            align="start"
            v-for="view in ['overview', 'properties', 'relationships']"
            :key="view"
            class="mt-1 w-full"
            :aria-pressed="tab === view"
            @click="tab = view"
          >
            {{ $t(`ontologyManager.views.${view}`) }}
          </UiButton>
        </div>
        <section
          v-if="resource"
          class="rounded-control border border-border bg-surface p-5"
        >
          <h2 class="text-xl font-semibold">{{ resource.display_name }}</h2>
          <p class="mt-1 text-xs text-muted">
            {{ resource.resource_id }} · {{ resource.kind }}
          </p>
          <p class="mt-3 text-sm">
            {{ properties.length }}{{ $t("ontologyManager.properties")
            }}{{ links.length }}{{ $t("ontologyManager.relationships") }}
          </p>
        </section>
        <LazyOntologyRelations
          v-if="tab === 'overview' || tab === 'relationships'"
          :nodes="graphNodes"
          :edges="graphEdges"
          @select="(value) => (selected = value)"
        />
        <section
          v-if="tab === 'overview' || tab === 'properties'"
          class="overflow-x-auto rounded-control border border-border bg-surface"
        >
          <h2 class="border-b border-border px-4 py-3 text-sm font-semibold">
            {{ $t("ontologyManager.properties_2") }}
          </h2>
          <table class="w-full text-left text-sm">
            <thead class="bg-surface-muted text-muted">
              <tr>
                <th class="p-3">{{ $t("ontologyManager.name") }}</th>
                <th class="p-3">{{ $t("ontologyManager.type") }}</th>
                <th class="p-3">
                  {{ $t("ontologyManager.identifier") }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="p in properties"
                :key="p.resource_id"
                class="border-t border-border"
              >
                <td class="p-3">{{ p.display_name }}</td>
                <td class="p-3">{{ p.value_type }}</td>
                <td class="p-3 text-muted">{{ p.resource_id }}</td>
              </tr>
            </tbody>
          </table>
          <div
            v-if="
              canManage &&
              !selectedPackage &&
              !document.plugin_origin &&
              selected
            "
            class="flex flex-wrap gap-2 border-t border-border p-3"
          >
            <UiField :label="$t('ontologyManager.property_name')" v-slot="field"
              ><UiInput
                v-model="newPropertyName"
                :id="field.id"
                :disabled="busy"
                :aria-label="$t('ontologyManager.property_name')"
                :placeholder="$t('ontologyManager.property_name')" /></UiField
            ><UiField
              :label="$t('ontologyManager.property_identifier')"
              v-slot="field"
              ><UiInput
                v-model="newPropertyId"
                :id="field.id"
                :disabled="busy"
                :aria-label="$t('ontologyManager.property_identifier')"
                :placeholder="
                  $t('ontologyManager.organization_person_name')
                " /></UiField
            ><UiField
              :label="$t('ontologyManager.property_type')"
              v-slot="field"
              ><UiSelect
                v-model="newPropertyType"
                :id="field.id"
                :disabled="busy"
                :aria-label="$t('ontologyManager.property_type')"
                :options="
                  ['string', 'integer', 'boolean', 'datetime', 'json'].map(
                    (value) => ({ value, label: value }),
                  )
                " /></UiField
            ><UiButton
              variant="secondary"
              :disabled="busy || !newPropertyName.trim()"
              @click="addProperty"
              >{{ $t("ontologyManager.add_property") }}</UiButton
            >
          </div>
        </section>
        <section
          v-if="selectedPackage"
          class="rounded-control border border-border bg-surface p-4"
          :aria-label="$t('ontologyManager.participant_mappings')"
        >
          <h2 class="font-semibold">
            {{ $t("ontologyManager.link_participants_to_your_types") }}
          </h2>
          <p class="my-2 text-sm text-muted">
            {{
              $t(
                "ontologyManager.plugin_types_remain_isolated_until_you_select_a_",
              )
            }}
          </p>
          <UiField :label="$t('ontologyManager.target_ontology')" v-slot="field"
            ><UiSelect
              :model-value="targetId"
              :id="field.id"
              :disabled="busy || !canManage"
              :aria-label="$t('ontologyManager.target_ontology')"
              :options="[
                { value: '', label: $t('ontologyManager.keepIsolated') },
                ...ontologies
                  .filter((o) => o.productionPublication && o.ontologyId !== id)
                  .map((o) => ({ value: o.ontologyId, label: o.displayName })),
              ]"
              @update:model-value="chooseTarget"
          /></UiField>
          <div class="mt-4 grid gap-3 sm:grid-cols-2">
            <label v-for="slot in slots" :key="slot.resource_id" class="text-sm"
              >{{ slot.display_name
              }}<UiSelect
                :disabled="busy || !canManage"
                :model-value="mappings[slot.resource_id] ?? ''"
                :aria-label="
                  $t('ontologyManager.participantType', {
                    name: slot.display_name,
                  })
                "
                :options="[
                  { value: '', label: $t('ontologyManager.isolatedPerson') },
                  ...targetTypes.map((r) => ({
                    value: r.resource_id,
                    label: r.display_name,
                  })),
                ]"
                @update:model-value="
                  (value) => setMapping(slot.resource_id, value)
                "
            /></label>
          </div>
        </section>
        <section
          v-if="
            canManage &&
            revision &&
            !dirty &&
            ontologies.find((o) => o.ontologyId === id)?.productionPublication
          "
          class="rounded-control border border-border bg-surface p-4"
          :aria-label="$t('ontologyManager.pipeline_ontology_binding')"
        >
          <h2 class="font-semibold">
            {{ $t("ontologyManager.use_in_a_pipeline") }}
          </h2>
          <p class="my-2 text-sm text-muted">
            {{
              $t(
                "ontologyManager.this_creates_a_new_pipeline_draft_deployed_revis",
              )
            }}
          </p>
          <div class="flex flex-wrap items-end gap-3">
            <UiSelect
              :model-value="pipelineId"
              :aria-label="$t('ontologyManager.pipeline')"
              :options="
                pipelines.map((p) => ({ value: p.pipeline_id, label: p.name }))
              "
              @update:model-value="choosePipeline"
            /><UiSelect
              v-model="blockId"
              :aria-label="$t('ontologyManager.pipeline_block')"
              :options="pipelineBlocks"
            /><UiButton
              :disabled="busy || !pipelineId || !blockId"
              @click="bind"
              >{{ $t("ontologyManager.link_to_block") }}</UiButton
            >
          </div>
        </section>
        <details
          class="rounded-control border border-border bg-surface p-4 text-sm"
        >
          <summary>
            {{ $t("ontologyManager.advanced_provenance_and_identifiers") }}
          </summary>
          <pre class="mt-3 overflow-x-auto text-xs">{{
            JSON.stringify(
              {
                origin: document.plugin_origin,
                target: document.target_origin,
                mappings,
              },
              null,
              2,
            )
          }}</pre>
        </details>
      </main>
      <main v-else class="p-8">
        <UiEmptyState
          :title="$t('ontologyManager.connect_your_business_concepts')"
          :description="
            $t(
              'ontologyManager.explore_a_plugin_model_or_create_an_ontology_for',
            )
          "
        />
      </main>
    </div>
    <UiDialog
      v-model:open="reviewing"
      :title="$t('ontologyManager.review_ontology_changes')"
      :description="
        $t('ontologyManager.saving_creates_a_tenant_only_draft_publication_i')
      "
      ><p class="mb-3 text-sm">
        {{ pending?.resources.length ?? 0 }}{{ $t("ontologyManager.resources")
        }}{{ Object.keys(mappings).length
        }}{{ $t("ontologyManager.explicit_participant_mappings") }}
      </p>
      <ul class="space-y-2 text-sm">
        <li v-for="(value, key) in mappings" :key="key">
          {{ key }} → {{ value }}
        </li>
      </ul>
      <template #footer
        ><UiButton variant="secondary" @click="reviewing = false">{{
          $t("ontologyManager.cancel")
        }}</UiButton
        ><UiButton :disabled="busy || !canManage" @click="save">{{
          $t("ontologyManager.save_draft")
        }}</UiButton></template
      ></UiDialog
    >
  </section>
</template>
