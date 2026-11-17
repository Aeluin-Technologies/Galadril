<script setup lang="ts">
import type { GolDiagnostic } from "~/types/ontology";
const props = defineProps<{ ontologyId?: string }>();
const {
  id,
  name,
  summary,
  latest,
  comparing,
  reviewLatest,
  keepDraft,
  files,
  current,
  source,
  activeFile,
  projection,
  validation,
  diagnostics,
  dependencies,
  schema,
  profile,
  fields,
  editing,
  busy,
  loading,
  failed,
  notice,
  canEdit,
  canPublish,
  canDelete,
  reviewing,
  deleting,
  dirty,
  valid,
  symbol,
  selected,
  graph,
  load,
  edit,
  save,
  validate,
  publish,
  remove,
  previewSchema,
  addFile,
  locate,
} = useOntologyWorkspace(props.ontologyId);
const sourcePanel = ref<HTMLElement | null>(null);
async function inspectDiagnostic(diagnostic: GolDiagnostic) {
  const position = locate(diagnostic);
  if (!position) return;
  await nextTick();
  const input = sourcePanel.value?.querySelector("textarea");
  input?.focus();
  input?.setSelectionRange(position.start, position.end);
}
watch(comparing, async (open) => {
  if (open) return;
  await nextTick();
  sourcePanel.value?.querySelector("textarea")?.focus();
});
function selectField(name: string, checked: boolean) {
  fields.value = checked
    ? [...new Set([...fields.value, name])]
    : fields.value.filter((value) => value !== name);
  schema.value = undefined;
}
</script>
<template>
  <section class="flex h-full min-h-0 flex-col overflow-hidden bg-canvas">
    <header
      class="shrink-0 border-b border-border bg-surface px-5 py-4 sm:px-8"
    >
      <UiPageHeader
        :title="
          name || $t(props.ontologyId ? 'gol.workspace' : 'gol.newOntology')
        "
        :description="$t('gol.workspaceHelp')"
      >
        <template #actions>
          <NuxtLink to="/ontology"
            ><UiButton variant="ghost">{{
              $t("gol.catalog")
            }}</UiButton></NuxtLink
          >
          <UiButton
            v-if="canEdit && !editing"
            variant="secondary"
            :disabled="busy || loading"
            :action="edit"
            >{{ $t("gol.edit") }}</UiButton
          >
          <UiButton
            v-if="editing"
            variant="secondary"
            :disabled="busy || loading || !dirty || !files.length"
            :action="save"
            >{{ $t("gol.save") }}</UiButton
          >
          <UiButton
            v-if="editing"
            variant="secondary"
            :disabled="busy || loading || dirty || !summary"
            :action="validate"
            >{{ $t("gol.validate") }}</UiButton
          >
          <UiButton
            v-if="editing"
            :disabled="busy || !canPublish || !valid"
            @click="reviewing = true"
            >{{ $t("gol.publish") }}</UiButton
          >
        </template>
      </UiPageHeader>
    </header>
    <div class="min-h-0 flex-1 overflow-auto p-5 sm:p-8">
      <div class="mx-auto max-w-7xl space-y-5">
        <p v-if="loading" role="status" class="text-sm text-muted">
          {{ $t("gol.loading") }}
        </p>
        <UiAlert v-if="failed" tone="danger">
          <p>{{ $t("gol.unavailable") }}</p>
          <p v-if="editing" class="mt-2">{{ $t("gol.draftRetained") }}</p>
          <div class="mt-2 flex flex-wrap gap-2">
            <UiButton
              variant="ghost"
              :disabled="busy || loading"
              :action="load"
              >{{ $t("gol.retry") }}</UiButton
            >
            <UiButton
              v-if="editing && summary && canEdit"
              variant="secondary"
              :disabled="busy || loading"
              :action="reviewLatest"
              >{{ $t("gol.reviewLatest") }}</UiButton
            >
          </div>
        </UiAlert>
        <p v-if="notice" role="status" class="text-sm text-success">
          {{ notice }}
        </p>
        <dl
          v-if="summary || projection"
          class="flex flex-wrap gap-5 rounded-panel border border-border bg-surface p-4 text-sm"
        >
          <div v-if="summary">
            <dt class="text-muted">{{ $t("gol.sourceRevision") }}</dt>
            <dd class="mt-1 break-all font-mono text-xs">
              {{ summary.sourceRevision }}
            </dd>
          </div>
          <div>
            <dt class="text-muted">{{ $t("gol.activeGeneration") }}</dt>
            <dd class="mt-1 break-all font-mono text-xs">
              {{
                summary?.activeGeneration ??
                projection?.generation ??
                $t("gol.notPublished")
              }}
            </dd>
          </div>
          <div v-if="validation">
            <dt class="text-muted">{{ $t("gol.validationStatus") }}</dt>
            <dd>{{ $t(`gol.validation.${validation.status}`) }}</dd>
          </div>
        </dl>
        <section
          v-if="editing && canEdit && !loading"
          class="space-y-4 rounded-panel border border-border bg-surface p-4"
          :aria-label="$t('gol.sourceEditor')"
        >
          <div class="grid gap-4 sm:grid-cols-2">
            <UiInput
              v-model="id"
              :label="$t('gol.id')"
              :disabled="busy || !!summary"
            />
            <UiInput v-model="name" :label="$t('gol.name')" :disabled="busy" />
          </div>
          <div class="flex flex-wrap items-end gap-3">
            <UiSelect
              v-model="activeFile"
              :label="$t('gol.file')"
              :disabled="busy"
              :options="
                files.map((file, index) => ({ value: index, label: file.path }))
              "
            />
            <UiButton
              variant="secondary"
              :disabled="busy || files.length >= 32"
              @click="addFile"
              >{{ $t("gol.addFile") }}</UiButton
            >
          </div>
          <UiInput
            v-if="current"
            v-model="current.path"
            :label="$t('gol.path')"
            :disabled="busy"
          />
          <div ref="sourcePanel">
            <UiTextarea
              v-model="source"
              :label="$t('gol.source')"
              :disabled="busy"
              class="min-h-[420px] font-mono text-xs"
              spellcheck="false"
            />
          </div>
          <p class="text-sm text-muted">{{ $t("gol.sourceHelp") }}</p>
          <UiButton
            v-if="canDelete && summary"
            variant="danger"
            :disabled="busy || dirty"
            @click="deleting = true"
            >{{ $t("gol.delete") }}</UiButton
          >
        </section>
        <section
          v-if="editing && diagnostics.length"
          class="rounded-panel border border-border bg-surface p-4"
          :aria-label="$t('gol.diagnostics')"
        >
          <h2 class="mb-3 font-semibold">{{ $t("gol.diagnostics") }}</h2>
          <ul class="space-y-2">
            <li
              v-for="(diagnostic, index) in diagnostics"
              :key="`${diagnostic.path}:${diagnostic.start}:${index}`"
            >
              <UiButton
                variant="ghost"
                align="start"
                class="w-full"
                @click="inspectDiagnostic(diagnostic)"
                ><span class="break-words text-left"
                  ><strong>{{ diagnostic.code }}</strong> ·
                  {{ diagnostic.path }} · {{ diagnostic.start }}–{{
                    diagnostic.end
                  }}<span class="block">{{ diagnostic.message }}</span></span
                ></UiButton
              >
            </li>
          </ul>
        </section>
        <section
          v-if="dependencies.length"
          class="rounded-panel border border-border bg-surface p-4"
          :aria-label="$t('gol.dependencies')"
        >
          <h2 class="mb-3 font-semibold">{{ $t("gol.dependencies") }}</h2>
          <ul class="space-y-2 text-sm">
            <li
              v-for="dependency in dependencies"
              :key="`${dependency.ontologyId}:${dependency.generation}`"
            >
              <NuxtLink
                :to="`/ontology/edit/${encodeURIComponent(dependency.ontologyId)}`"
                class="text-primary-foreground"
                >{{ dependency.ontologyId }}</NuxtLink
              ><span class="ml-2 break-all font-mono text-xs text-muted">{{
                dependency.generation
              }}</span>
            </li>
          </ul>
        </section>
        <template v-if="projection">
          <section
            class="grid gap-5 lg:grid-cols-[240px_minmax(0,1fr)]"
            :aria-label="$t('gol.symbols')"
          >
            <nav
              class="rounded-panel border border-border bg-surface p-3"
              :aria-label="$t('gol.symbols')"
            >
              <UiButton
                v-for="item in projection.symbols"
                :key="item.key"
                variant="ghost"
                align="start"
                class="w-full"
                :aria-pressed="symbol?.key === item.key"
                @click="selected = item.key"
                >{{ item.name }}</UiButton
              >
            </nav>
            <section
              v-if="symbol"
              class="min-w-0 space-y-4 rounded-panel border border-border bg-surface p-4"
            >
              <h2 class="text-lg font-semibold">{{ symbol.name }}</h2>
              <p class="break-all text-xs text-muted">
                {{ symbol.kind }} · {{ symbol.key }}
              </p>
              <table
                v-if="symbol.properties.length"
                class="w-full text-left text-sm"
              >
                <thead>
                  <tr>
                    <th class="p-2">{{ $t("gol.property") }}</th>
                    <th class="p-2">{{ $t("gol.type") }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="property in symbol.properties"
                    :key="property.key"
                    class="border-t border-border"
                  >
                    <td class="p-2">{{ property.name }}</td>
                    <td class="p-2 font-mono text-xs">
                      {{ property.typeName }}
                    </td>
                  </tr>
                </tbody>
              </table>
              <div v-if="symbol.references.length">
                <h3 class="font-semibold">{{ $t("gol.references") }}</h3>
                <ul class="mt-2 space-y-1 text-xs">
                  <li
                    v-for="reference in symbol.references"
                    :key="reference"
                    class="break-all font-mono"
                  >
                    {{ reference }}
                  </li>
                </ul>
              </div>
              <UiSelect
                v-model="profile"
                :label="$t('gol.profile')"
                :options="
                  (['STANDARD', 'OPEN_AI_STRICT', 'OUTLINES'] as const).map(
                    (value) => ({ value, label: $t(`gol.profiles.${value}`) }),
                  )
                "
              />
              <div v-if="symbol.properties.length" class="flex flex-wrap gap-3">
                <UiCheckbox
                  v-for="property in symbol.properties"
                  :key="property.key"
                  :label="property.name"
                  :model-value="fields.includes(property.name)"
                  @update:model-value="
                    (value) => selectField(property.name, value === true)
                  "
                />
              </div>
              <UiButton
                variant="secondary"
                :disabled="busy"
                :action="previewSchema"
                >{{ $t("gol.previewSchema") }}</UiButton
              >
              <p class="text-xs text-muted">{{ $t("gol.schemaHelp") }}</p>
            </section>
          </section>
          <LazyOntologyRelations
            v-if="projection.symbols.length"
            :nodes="graph.nodes"
            :edges="graph.edges"
            @select="selected = $event"
          />
        </template>
        <UiEmptyState
          v-else-if="!loading && !failed && !editing"
          :title="$t('gol.noSymbols')"
          :description="$t('gol.noSymbolsHelp')"
        />
        <section
          v-if="schema !== undefined"
          class="rounded-panel border border-border bg-surface p-4"
          :aria-label="$t('gol.schema')"
        >
          <h2 class="mb-3 font-semibold">{{ $t("gol.schema") }}</h2>
          <pre
            class="max-h-[480px] overflow-auto whitespace-pre-wrap break-words text-xs"
            >{{ JSON.stringify(schema, null, 2) }}</pre>
        </section>
      </div>
    </div>
    <OntologySourceComparison
      v-if="latest"
      v-model:open="comparing"
      :latest="latest.files"
      :draft="files"
      @keep="keepDraft"
    />
    <UiDialog
      v-model:open="reviewing"
      :title="$t('gol.review')"
      :description="$t('gol.reviewHelp')"
      ><div class="space-y-4">
        <section v-for="file in files" :key="file.path">
          <h3 class="font-semibold">{{ file.path }}</h3>
          <pre
            class="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-xs"
            >{{ file.text }}</pre>
        </section>
      </div>
      <template #footer
        ><UiButton variant="secondary" @click="reviewing = false">{{
          $t("gol.cancel")
        }}</UiButton
        ><UiButton :disabled="busy || !valid" :action="publish">{{
          $t("gol.confirmPublish")
        }}</UiButton></template
      ></UiDialog
    >
    <UiDialog
      v-model:open="deleting"
      :title="$t('gol.delete')"
      :description="$t('gol.deleteHelp')"
      ><template #footer
        ><UiButton variant="secondary" @click="deleting = false">{{
          $t("gol.cancel")
        }}</UiButton
        ><UiButton
          variant="danger"
          :disabled="busy || dirty"
          :action="remove"
          >{{ $t("gol.delete") }}</UiButton
        ></template
      ></UiDialog
    >
  </section>
</template>
