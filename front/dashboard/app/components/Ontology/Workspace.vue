<script setup lang="ts">
import {
  PlusIcon,
  TrashIcon,
  ArrowUturnLeftIcon,
  PencilSquareIcon,
  CubeIcon,
  LinkIcon,
  ListBulletIcon,
  Squares2X2Icon,
  ChevronRightIcon,
} from "@heroicons/vue/24/outline";
import { ontologyPrimaryAction, withOntologyLink } from "~/utils/ontology";
import {
  withUpdatedOntologyLink,
  withoutOntologyLink,
} from "~/utils/ontology-edit";
import type {
  OntologyTypeDraft,
  OntologyLinkDraft,
} from "~/utils/ontology-edit";
const { t } = useI18n();
const mode = ref("graph");
const panel = ref("editor");
const deleting = ref(false);
const editing = ref(false);
const creatingType = ref(false);
const editedLink = ref<OntologyLinkDraft>();
const props = defineProps<{ ontologyId?: string }>();
const inspectedProperty = ref("");
const propertyDetails = computed(() =>
  properties.value.find((item) => item.resource_id === inspectedProperty.value),
);
const {
  artifact,
  branch,
  deleted,
  canDelete,
  checkout,
  author,
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
  published,
  targetId,
  targetRevision,
  mappings,
  selected,
  search,
  tab,
  busy,
  error,
  reviewing,
  pending,
  dirty,
  types,
  properties,
  resource,
  links,
  slots,
  targetTypes,
  graphNodes,
  graphEdges,
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
  save,
  addType,
  addProperty,
} = useOntologyWorkspace(props.ontologyId);
watch(id, () => {
  panel.value = "editor";
  deleting.value = false;
});
watch([revision, branch], () => {
  editing.value = false;
});
watch(
  () => document.value,
  () => {
    if (!document.value) {
      creatingType.value = false;
      editedLink.value = undefined;
      editing.value = false;
      return;
    }
    if (
      document.value &&
      !revision.value &&
      !selectedPackage.value &&
      !document.value.plugin_origin
    )
      editing.value = true;
  },
);
const primary = computed(() =>
  editing.value
    ? "review"
    : ontologyPrimaryAction({
        revision: revision.value,
        dirty: dirty.value,
        published: branch.value !== "main" || published.value,
      }),
);
const graphEditable = computed(
  () =>
    editing.value &&
    canManage.value &&
    !!document.value &&
    !selectedPackage.value &&
    !document.value.plugin_origin,
);
async function primaryAction() {
  if (primary.value === "publish") await publish();
  else if (primary.value === "edit") editing.value = true;
  else await review();
}
function connect(source: string, target: string) {
  if (!graphEditable.value) return;
  error.value = "";
  editedLink.value = {
    id: `relation.${crypto.randomUUID().replaceAll("-", "")}`,
    source,
    target,
    name: "",
    existing: false,
  };
}
function editLink(id: string) {
  if (!graphEditable.value) return;
  const link = document.value?.resources.find(
    (resource) => resource.resource_id === id && resource.kind === "link_type",
  );
  if (
    !link ||
    typeof link.attributes.source_type !== "string" ||
    typeof link.attributes.target_type !== "string"
  )
    return;
  error.value = "";
  editedLink.value = {
    id,
    source: link.attributes.source_type,
    target: link.attributes.target_type,
    name: link.display_name,
    existing: true,
  };
}
function saveLink(link: OntologyLinkDraft) {
  if (!graphEditable.value || !document.value) return;
  try {
    const base = link.existing
      ? document.value
      : withOntologyLink(document.value, link.id, link.source, link.target);
    document.value = withUpdatedOntologyLink(
      base,
      link.id,
      link.source,
      link.target,
      link.name,
    );
    editedLink.value = undefined;
  } catch {
    error.value = t("ontologyManager.invalidConnection");
  }
}
function removeLink(id: string) {
  if (!graphEditable.value || !document.value) return;
  try {
    document.value = withoutOntologyLink(document.value, id);
    editedLink.value = undefined;
  } catch {
    error.value = t("ontologyManager.invalidConnection");
  }
}
function createType(input: OntologyTypeDraft) {
  if (graphEditable.value && addType(input)) creatingType.value = false;
}
function selectResource(value: string) {
  panel.value = "editor";

  selected.value = selected.value === value ? "" : value;
  if (!selected.value) tab.value = "overview";
  mode.value = "graph";
}
</script>
<template>
  <section
    class="flex h-full min-h-0 flex-col overflow-hidden bg-canvas text-foreground"
    :aria-label="$t('ontologyManager.ontology_manager')"
  >
    <header
      class="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-5 py-3"
    >
      <div>
        <NuxtLink
          to="/ontology"
          class="text-xs text-muted hover:text-primary-foreground"
          >{{ $t("ontologyManager.allOntologies") }}</NuxtLink
        >
        <h1 class="sr-only">{{ $t("ontologyManager.ontology_manager") }}</h1>
        <nav
          class="flex min-w-0 items-center gap-2"
          :aria-label="$t('ontologyManager.location')"
        >
          <UiButton
            variant="ghost"
            align="start"
            :aria-current="!selected ? 'page' : undefined"
            @click="selectResource('')"
            ><Squares2X2Icon
              class="mr-2 h-4 w-4 shrink-0"
              aria-hidden="true"
            />{{ name || $t("ontologyManager.ontology_manager") }}</UiButton
          >
          <template v-if="resource"
            ><ChevronRightIcon
              class="h-4 w-4 shrink-0 text-muted"
              aria-hidden="true"
            /><span
              aria-current="page"
              class="truncate text-sm font-semibold"
              >{{ resource.display_name }}</span
            ></template
          >
        </nav>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <UiIconButton
          v-if="dirty"
          :icon="ArrowUturnLeftIcon"
          :label="$t('ontologyManager.discard_changes')"
          :disabled="busy"
          variant="ghost"
          @click="discard"
        />
        <UiIconButton
          :icon="PlusIcon"
          :label="$t('ontologyManager.new_ontology')"
          :disabled="busy || dirty || !canCreate"
          variant="ghost"
          @click="
            newOntology();
            editing = true;
          "
        />
        <UiIconButton
          v-if="
            revision &&
            canDelete &&
            !selectedPackage &&
            !document?.plugin_origin &&
            !deleted
          "
          :icon="TrashIcon"
          :label="$t('ontologyManager.collaboration.deleteOntology')"
          :disabled="busy || dirty"
          @click="deleting = true"
        />
        <UiButton
          :loading="busy"
          :disabled="
            busy ||
            !document ||
            deleted ||
            !name.trim() ||
            (primary === 'publish' ? !canPublish : !canManage) ||
            (primary === 'review' && !!revision && !dirty)
          "
          @click="primaryAction"
          >{{
            $t(
              primary === "publish"
                ? "ontologyManager.publish_ontology"
                : primary === "edit"
                  ? "ontologyManager.editOntology"
                  : "ontologyManager.review_changes",
            )
          }}</UiButton
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
    <p v-if="error" role="alert" class="px-5 text-sm text-danger">
      {{ error }}
    </p>
    <div
      class="grid min-h-0 flex-1 overflow-auto lg:grid-cols-[250px_minmax(0,1fr)] lg:overflow-hidden"
    >
      <OntologyCatalog
        v-model:search="search"
        v-model:view="tab"
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
        @select="selectResource"
      />
      <main
        v-if="document"
        class="min-h-0 min-w-0 space-y-4 p-4 sm:p-6 lg:overflow-y-auto"
      >
        <nav
          class="flex flex-wrap items-center gap-2 border-b border-border pb-3"
          :aria-label="$t('ontologyManager.collaboration.navigation')"
        >
          <UiButton
            v-for="view in ['editor', 'branches', 'history']"
            :key="view"
            variant="ghost"
            :aria-pressed="panel === view"
            :disabled="view !== 'editor' && !revision"
            @click="panel = view"
            >{{ $t(`ontologyManager.collaboration.${view}`) }}</UiButton
          >
          <span v-if="revision" class="ml-auto text-xs text-muted">{{
            branch
          }}</span>
        </nav>
        <div
          v-if="deleted"
          class="space-y-3 rounded-panel border border-border bg-surface p-4"
        >
          <p class="text-sm text-muted">
            {{ $t("ontologyManager.collaboration.deleted") }}
          </p>
          <UiButton
            v-if="canManage"
            :disabled="busy"
            @click="author({ operation: 'restore' })"
            >{{ $t("ontologyManager.collaboration.restore") }}</UiButton
          >
        </div>
        <OntologyCollaboration
          v-if="panel !== 'editor' && artifact"
          :key="id"
          :id="id"
          :artifact="artifact"
          :branch="branch"
          :mode="panel === 'history' ? 'history' : 'branches'"
          :disabled="busy || dirty || deleted"
          :writable="canManage && !deleted"
          :error="error"
          :command="author"
          @checkout="checkout"
        />
        <div v-if="panel === 'editor' && !deleted" class="space-y-4">
          <nav
            class="flex flex-wrap items-center gap-2 border-b border-border pb-3"
            :aria-label="$t('ontologyManager.editorViews')"
          >
            <UiButton
              v-for="view in ['graph', 'code']"
              :key="view"
              variant="ghost"
              :aria-pressed="mode === view"
              @click="mode = view"
              >{{ $t(`pipeline.review.${view}`) }}</UiButton
            >
            <UiButton
              v-if="canManage && !selectedPackage && !document.plugin_origin"
              variant="secondary"
              class="ml-auto"
              :aria-pressed="editing"
              @click="editing = !editing"
              ><PencilSquareIcon class="mr-2 h-4 w-4" aria-hidden="true" />{{
                $t("ontologyManager.editGraph")
              }}</UiButton
            >
          </nav>
          <OntologyCode
            v-show="mode === 'code'"
            :document="document"
            :editable="graphEditable"
            :selected="selected"
            :active="mode === 'code'"
            @apply="(value) => (document = value)"
          />
          <template v-if="mode === 'graph'">
            <div class="grid gap-3 sm:grid-cols-2">
              <UiField
                :label="$t('ontologyManager.ontology_name')"
                v-slot="field"
                ><UiInput
                  v-model="name"
                  :id="field.id"
                  :disabled="busy || !canManage || (!editing && !!revision)"
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
            <div v-if="graphEditable" class="flex flex-wrap gap-2">
              <UiButton
                variant="secondary"
                @click="
                  error = '';
                  creatingType = true;
                "
                ><PlusIcon class="mr-2 h-4 w-4" aria-hidden="true" />{{
                  $t("ontologyManager.add_object_type")
                }}</UiButton
              >
              <UiButton
                variant="secondary"
                @click="connect(selected || types[0]?.resource_id || '', '')"
                ><LinkIcon class="mr-2 h-4 w-4" aria-hidden="true" />{{
                  $t("ontologyManager.relationshipEditor.add")
                }}</UiButton
              >
            </div>
            <section
              v-if="!resource"
              class="rounded-panel border border-border bg-surface p-5"
            >
              <h2 class="flex items-center gap-2 text-xl font-semibold">
                <Squares2X2Icon
                  class="h-6 w-6 text-primary-foreground"
                  aria-hidden="true"
                />{{ name }}
              </h2>
              <p class="mt-2 text-sm text-muted">
                {{ $t("ontologyManager.completeOverview") }}
              </p>
              <div class="mt-4 flex flex-wrap gap-5 text-sm">
                <span class="flex items-center gap-2"
                  ><CubeIcon class="h-4 w-4" aria-hidden="true" />{{
                    $t("ontologyManager.typeCount", types.length)
                  }}</span
                ><span class="flex items-center gap-2"
                  ><ListBulletIcon class="h-4 w-4" aria-hidden="true" />{{
                    $t("ontologyManager.propertyCount", properties.length)
                  }}</span
                ><span class="flex items-center gap-2"
                  ><LinkIcon class="h-4 w-4" aria-hidden="true" />{{
                    $t("ontologyManager.linkCount", links.length)
                  }}</span
                >
              </div>
            </section>
            <section
              v-if="resource"
              class="rounded-control border border-border bg-surface p-5"
            >
              <UiField
                v-if="graphEditable"
                :label="$t('ontologyManager.object_type_name')"
                v-slot="field"
                ><UiInput :id="field.id" v-model="resource.display_name"
              /></UiField>
              <h2 class="flex items-center gap-3 text-xl font-semibold">
                <CubeIcon
                  class="h-8 w-8 text-primary-foreground"
                  aria-hidden="true"
                />{{ resource.display_name }}
              </h2>
              <p class="mt-1 text-xs text-muted">
                {{ resource.resource_id }} · {{ resource.kind }}
              </p>
              <div class="mt-4 grid gap-3 sm:grid-cols-3">
                <div class="rounded-control border border-border p-3">
                  <ListBulletIcon
                    class="mb-2 h-4 w-4 text-muted"
                    aria-hidden="true"
                  />{{ $t("ontologyManager.properties_2")
                  }}<strong class="block text-2xl">{{
                    properties.length
                  }}</strong>
                </div>
                <div class="rounded-control border border-border p-3">
                  <ListBulletIcon
                    class="mb-2 h-4 w-4 text-muted"
                    aria-hidden="true"
                  />{{ $t("ontologyManager.views.relationships")
                  }}<strong class="block text-2xl">{{ links.length }}</strong>
                </div>
                <div class="rounded-control border border-border p-3">
                  <ListBulletIcon
                    class="mb-2 h-4 w-4 text-muted"
                    aria-hidden="true"
                  />{{ $t("ontologyManager.type")
                  }}<span class="mt-1 block text-sm">{{ resource.kind }}</span>
                </div>
              </div>
              <p
                v-if="typeof resource.attributes.description === 'string'"
                class="mt-4 text-sm text-muted"
              >
                {{ resource.attributes.description }}
              </p>
            </section>
            <LazyOntologyRelations
              v-if="tab === 'overview' || tab === 'relationships'"
              :key="selected"
              :nodes="graphNodes"
              :editable="graphEditable"
              @connect="connect"
              @inspect="editLink"
              :edges="graphEdges"
              @select="selectResource"
            />
            <section
              v-if="(resource && tab === 'overview') || tab === 'properties'"
              class="overflow-x-auto rounded-control border border-border bg-surface"
            >
              <h2
                class="border-b border-border px-4 py-3 text-sm font-semibold"
              >
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
                    <td class="p-3">
                      <UiButton
                        variant="ghost"
                        align="start"
                        @click="inspectedProperty = p.resource_id"
                        ><ListBulletIcon
                          aria-hidden="true"
                          class="mr-2 h-4 w-4"
                        />{{ p.display_name }}</UiButton
                      >
                    </td>
                    <td class="p-3">
                      <OntologyValueType :value="p.value_type ?? p.kind" />
                    </td>
                    <td class="p-3 text-muted">{{ p.resource_id }}</td>
                  </tr>
                </tbody>
              </table>
              <div
                v-if="graphEditable && selected"
                class="flex flex-wrap gap-2 border-t border-border p-3"
              >
                <UiField
                  :label="$t('ontologyManager.property_name')"
                  v-slot="field"
                  ><UiInput
                    v-model="newPropertyName"
                    :id="field.id"
                    :disabled="busy"
                    :aria-label="$t('ontologyManager.property_name')"
                    :placeholder="
                      $t('ontologyManager.property_name')
                    " /></UiField
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
            <details
              v-if="selectedPackage && !selected && tab === 'overview'"
              class="rounded-control border border-border bg-surface p-4"
              :aria-label="$t('ontologyManager.participant_mappings')"
            >
              <summary class="cursor-pointer font-semibold">
                {{ $t("ontologyManager.contactConnections") }}
              </summary>
              <p class="my-2 text-sm text-muted">
                {{ $t("ontologyManager.contactConnectionsHelp") }}
              </p>
              <UiField
                :label="$t('ontologyManager.target_ontology')"
                v-slot="field"
                ><UiSelect
                  :model-value="targetId"
                  :id="field.id"
                  :disabled="busy || !canManage || (!editing && !!revision)"
                  :aria-label="$t('ontologyManager.target_ontology')"
                  :options="[
                    { value: '', label: $t('ontologyManager.keepIsolated') },
                    ...ontologies
                      .filter(
                        (o) => o.productionPublication && o.ontologyId !== id,
                      )
                      .map((o) => ({
                        value: o.ontologyId,
                        label: o.displayName,
                      })),
                  ]"
                  @update:model-value="chooseTarget"
              /></UiField>
              <div class="mt-4 grid gap-3 sm:grid-cols-2">
                <label
                  v-for="slot in slots"
                  :key="slot.resource_id"
                  class="text-sm"
                  >{{ slot.display_name
                  }}<UiSelect
                    :disabled="busy || !canManage || (!editing && !!revision)"
                    :model-value="mappings[slot.resource_id] ?? ''"
                    :aria-label="
                      $t('ontologyManager.participantType', {
                        name: slot.display_name,
                      })
                    "
                    :options="[
                      {
                        value: '',
                        label: $t('ontologyManager.isolatedPerson'),
                      },
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
            </details>
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
          </template>
        </div>
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
      v-model:open="deleting"
      :title="$t('ontologyManager.collaboration.deleteOntology')"
      :description="$t('ontologyManager.collaboration.deleteHelp')"
    >
      <p v-if="error" role="alert" class="text-sm text-danger">{{ error }}</p>
      <template #footer
        ><UiButton variant="secondary" @click="deleting = false">{{
          $t("ontologyManager.cancel")
        }}</UiButton
        ><UiButton
          variant="danger"
          :disabled="busy || dirty"
          @click="
            async () => {
              if (await author({ operation: 'delete' })) deleting = false;
            }
          "
          >{{ $t("ontologyManager.collaboration.deleteOntology") }}</UiButton
        ></template
      >
    </UiDialog>
    <OntologyObjectWizard
      v-model:open="creatingType"
      :error="error"
      @create="createType"
    />
    <OntologyRelationshipEditor
      :draft="editedLink"
      :types="types"
      :error="error"
      @save="saveLink"
      @remove="removeLink"
      @close="editedLink = undefined"
    />
    <UiDialog
      v-model:open="reviewing"
      :title="$t('ontologyManager.review_ontology_changes')"
      :description="
        $t('ontologyManager.saving_creates_a_tenant_only_draft_publication_i')
      "
      ><p class="mb-3 text-sm">
        <span
          >{{ pending?.resources.length ?? 0 }}
          {{ $t("ontologyManager.resources") }}</span
        >
        <span class="mx-2" aria-hidden="true">·</span>
        <span
          >{{ Object.keys(mappings).length }}
          {{ $t("ontologyManager.explicit_participant_mappings") }}</span
        >
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
        ><UiButton
          :disabled="busy || !canManage || (!editing && !!revision)"
          @click="save"
          >{{ $t("ontologyManager.save_draft") }}</UiButton
        ></template
      ></UiDialog
    >
    <UiDialog
      :open="!!propertyDetails"
      :title="propertyDetails?.display_name ?? ''"
      :description="propertyDetails?.resource_id"
      @update:open="
        (value) => {
          if (!value) inspectedProperty = '';
        }
      "
    >
      <dl v-if="propertyDetails" class="space-y-4 text-sm">
        <div>
          <dt class="text-xs text-muted">{{ $t("ontologyManager.type") }}</dt>
          <dd class="mt-1">
            <OntologyValueType
              :value="propertyDetails.value_type ?? propertyDetails.kind"
            />
          </dd>
        </div>
        <div v-if="typeof propertyDetails.attributes.description === 'string'">
          <dt class="text-xs text-muted">
            {{ $t("ontologyManager.resourceDescription") }}
          </dt>
          <dd>{{ propertyDetails.attributes.description }}</dd>
        </div>
        <div>
          <dt class="text-xs text-muted">
            {{ $t("ontologyManager.resourceOwner") }}
          </dt>
          <dd>{{ propertyDetails.owner_id ?? "—" }}</dd>
        </div>
        <div>
          <dt class="text-xs text-muted">
            {{ $t("ontologyManager.resourceReferences") }}
          </dt>
          <dd
            v-for="reference in propertyDetails.references"
            :key="reference"
            class="mt-1 break-all font-mono text-xs"
          >
            {{ reference }}
          </dd>
          <dd v-if="!propertyDetails.references.length">—</dd>
        </div>
      </dl>
    </UiDialog>
  </section>
</template>
