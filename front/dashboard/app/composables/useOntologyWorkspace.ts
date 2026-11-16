/** Keeps full-source authoring separate from revocable runtime projections. */
import type {
  GolSourceFile,
  GolSummary,
  GolSymbols,
  GolValidation,
  GolWorkspace,
  GolSchemaProfile,
  GolDiagnostic,
} from "~/types/ontology";
import {
  diagnosticPosition,
  golDraftSource,
  sourceChanged,
} from "~/utils/ontology";
import { ontologyGraph } from "~/utils/ontology-graph";

export function useOntologyWorkspace(ontologyId?: string) {
  const api = useOntologyCatalog(),
    { t } = useI18n(),
    { session } = useAccount();
  const id = ref(ontologyId ?? "tenant.example"),
    name = ref(ontologyId ? "" : t("gol.newOntology"));
  const summary = shallowRef<GolSummary>();
  const files = ref<GolSourceFile[]>([]),
    saved = shallowRef<GolSourceFile[]>([]);
  const savedName = ref("");
  const activeFile = ref(0),
    selected = ref("");
  const projection = shallowRef<GolSymbols>();
  const validation = shallowRef<GolValidation>();
  const latest = shallowRef<GolWorkspace>();
  const comparing = ref(false);
  const diagnostics = ref<GolDiagnostic[]>([]),
    dependencies = ref<GolWorkspace["dependencies"]>([]);
  const schema = shallowRef<unknown>(),
    profile = ref<GolSchemaProfile>("STANDARD");
  const fields = ref<string[]>([]);
  const editing = ref(!ontologyId),
    busy = ref(false),
    loading = ref(false),
    failed = ref(false);
  const notice = ref(""),
    canEdit = ref(false),
    canPublish = ref(false),
    canDelete = ref(false);
  const reviewing = ref(false),
    deleting = ref(false);
  let epoch = 0;
  const current = computed(() => files.value[activeFile.value]);
  const source = computed({
    get: () => current.value?.text ?? "",
    set: (text: string) => {
      if (current.value) current.value.text = text;
    },
  });
  const dirty = computed(
    () =>
      name.value !== savedName.value || sourceChanged(files.value, saved.value),
  );
  const valid = computed(
    () =>
      validation.value?.status === "valid" &&
      validation.value.sourceRevision === summary.value?.sourceRevision &&
      !dirty.value,
  );
  const symbol = computed(
    () =>
      projection.value?.symbols.find((value) => value.key === selected.value) ??
      projection.value?.symbols[0],
  );
  const graph = computed(() =>
    ontologyGraph(projection.value?.symbols ?? [], symbol.value?.key),
  );
  watch(
    [files, name],
    () => {
      validation.value = undefined;
    },
    { deep: true },
  );
  watch(symbol, (value) => {
    schema.value = undefined;
    fields.value = value?.properties.map((property) => property.name) ?? [];
  });
  watch(profile, () => {
    schema.value = undefined;
  });

  function clear() {
    epoch++;
    api.cancel();
    files.value = [];
    saved.value = [];
    savedName.value = "";
    latest.value = undefined;
    comparing.value = false;
    id.value = ontologyId ?? "tenant.example";
    name.value = ontologyId ? "" : t("gol.newOntology");
    selected.value = "";
    fields.value = [];
    activeFile.value = 0;
    projection.value = undefined;
    summary.value = undefined;
    validation.value = undefined;
    diagnostics.value = [];
    dependencies.value = [];
    schema.value = undefined;
    canEdit.value = false;
    canPublish.value = false;
    canDelete.value = false;
    busy.value = false;
    loading.value = false;
    reviewing.value = false;
    deleting.value = false;
    editing.value = !ontologyId;
    notice.value = "";
  }
  function apply(value: GolWorkspace) {
    summary.value = value.summary;
    id.value = value.summary.ontologyId;
    name.value = value.summary.displayName;
    files.value = value.files.map((file) => ({ ...file }));
    saved.value = value.files.map((file) => ({ ...file }));
    savedName.value = value.summary.displayName;
    diagnostics.value = value.diagnostics;
    dependencies.value = value.dependencies;
    activeFile.value = Math.min(
      activeFile.value,
      Math.max(0, files.value.length - 1),
    );
  }
  /** A failed live read cannot keep a stale authorization projection visible. */
  function discardRuntime() {
    projection.value = undefined;
    schema.value = undefined;
    selected.value = "";
    fields.value = [];
    if (!editing.value) {
      summary.value = undefined;
      name.value = ontologyId ? "" : t("gol.newOntology");
    }
  }
  async function perform(action: (request: number) => Promise<void>) {
    if (busy.value) return;
    const request = epoch;
    busy.value = true;
    failed.value = false;
    notice.value = "";
    try {
      await action(request);
    } catch {
      if (request === epoch) failed.value = true;
    } finally {
      if (request === epoch) busy.value = false;
    }
  }
  async function metadata(request: number) {
    const value = await api.symbols(
      id.value,
      summary.value?.activeGeneration ?? undefined,
    );
    if (request !== epoch) return;
    if (!value.symbols.length && !editing.value)
      throw new Error("GOL_UNAVAILABLE");
    projection.value = value;
    if (!value.symbols.some((item) => item.key === selected.value))
      selected.value = value.symbols[0]?.key ?? "";
  }
  async function load() {
    const request = ++epoch;
    const target = summary.value?.ontologyId ?? ontologyId;
    const authoring = editing.value && !!summary.value;
    api.cancel();
    loading.value = true;
    failed.value = false;
    try {
      const permissions = await Promise.all([
        api.canManage(target).catch(() => false),
        target
          ? api.canManage(target, "PUBLISH").catch(() => false)
          : Promise.resolve(false),
        target
          ? api.canManage(target, "DELETE").catch(() => false)
          : Promise.resolve(false),
      ]);
      if (request !== epoch) return;
      [canEdit.value, canPublish.value, canDelete.value] = permissions;
      if (!target) {
        if (!canEdit.value) throw new Error("GOL_UNAVAILABLE");
        if (!files.value.length)
          files.value = [{ path: "main.gol", text: golDraftSource(id.value) }];
        return;
      }
      const catalog = await api.browse().catch(() => []);
      if (request !== epoch) return;
      if (!authoring) {
        summary.value = catalog.find((value) => value.ontologyId === target);
        name.value = summary.value?.displayName ?? "";
      }
      if (!summary.value || summary.value.activeGeneration) {
        await metadata(request);
        if (request === epoch && !summary.value) name.value = target;
      }
    } catch {
      if (request === epoch) {
        discardRuntime();
        failed.value = true;
      }
    } finally {
      if (request === epoch) loading.value = false;
    }
  }
  async function edit() {
    await perform(async (request) => {
      const value = await api.workspace(id.value);
      if (request !== epoch) return;
      apply(value);
      editing.value = true;
    });
  }
  async function reviewLatest() {
    if (!editing.value || !summary.value || !canEdit.value) return;
    await perform(async (request) => {
      const value = await api.workspace(id.value);
      if (request !== epoch) return;
      if (value.summary.ontologyId !== id.value)
        throw new Error("GOL_UNAVAILABLE");
      latest.value = value;
      comparing.value = true;
    });
  }
  /** A reviewed rebase never saves or publishes implicitly. */
  function keepDraft() {
    const value = latest.value;
    if (busy.value || !value || value.summary.ontologyId !== id.value) return;
    summary.value = value.summary;
    saved.value = value.files.map((file) => ({ ...file }));
    savedName.value = value.summary.displayName;
    diagnostics.value = [];
    dependencies.value = [];
    validation.value = undefined;
    discardRuntime();
    latest.value = undefined;
    comparing.value = false;
    failed.value = false;
    notice.value = t("gol.draftRebased");
  }
  async function save() {
    await perform(async (request) => {
      const value = await api.save(
        id.value,
        name.value,
        files.value,
        summary.value?.sourceRevision,
      );
      if (request !== epoch) return;
      apply(value);
      notice.value = t("gol.saved");
      const permission = await api
        .canManage(id.value, "PUBLISH")
        .catch(() => false);
      if (request === epoch) canPublish.value = permission;
    });
  }
  async function validate() {
    await perform(async (request) => {
      const revision = summary.value?.sourceRevision;
      if (!revision || dirty.value) return;
      const value = await api.validate(id.value, revision);
      if (request !== epoch) return;
      validation.value = value;
      diagnostics.value = value.diagnostics;
      dependencies.value = value.dependencies;
      notice.value = t(`gol.validation.${value.status}`);
    });
  }
  async function publish() {
    await perform(async (request) => {
      const revision = summary.value?.sourceRevision;
      if (!revision || !valid.value) return;
      const result = await api.publish(
        id.value,
        revision,
        summary.value?.activeGeneration ?? undefined,
      );
      if (request !== epoch || !summary.value) return;
      summary.value = {
        ...summary.value,
        activeGeneration: result.generation,
        activeSourceRevision: result.sourceRevision,
        status: "published",
      };
      reviewing.value = false;
      notice.value = t("gol.published");
      await metadata(request);
    });
  }
  async function remove() {
    await perform(async (request) => {
      const revision = summary.value?.sourceRevision;
      if (!revision) return;
      await api.remove(id.value, revision);
      if (request === epoch) await navigateTo("/ontology");
    });
  }
  async function previewSchema() {
    schema.value = undefined;
    await perform(async (request) => {
      const generation = projection.value?.generation,
        target = symbol.value?.key;
      if (!generation || !target) return;
      try {
        const value = await api.schema(
          id.value,
          generation,
          target,
          profile.value,
          fields.value,
        );
        if (request === epoch) schema.value = value;
      } catch (error) {
        if (request === epoch) discardRuntime();
        throw error;
      }
    });
  }
  function addFile() {
    if (files.value.length >= 32) return;
    const path = `module_${files.value.length + 1}.gol`;
    files.value.push({ path, text: "" });
    activeFile.value = files.value.length - 1;
  }
  function locate(diagnostic: GolDiagnostic) {
    const index = files.value.findIndex(
      (file) => file.path === diagnostic.path,
    );
    if (index < 0) return;
    activeFile.value = index;
    return diagnosticPosition(
      files.value[index]?.text ?? "",
      diagnostic.start,
      diagnostic.end,
    );
  }
  watch(comparing, (value) => {
    if (!value) latest.value = undefined;
  });
  onActivated(() => {
    void load();
  });
  onDeactivated(clear);
  onBeforeUnmount(clear);
  watch(
    () => JSON.stringify([session.value?.tenant, session.value?.subject]),
    () => {
      clear();
      void load();
    },
  );
  const invalidate = () => {
    clear();
    void load();
  };
  onMounted(() =>
    window.addEventListener("pengolo-access-invalidated", invalidate),
  );
  onBeforeUnmount(() =>
    window.removeEventListener("pengolo-access-invalidated", invalidate),
  );
  return {
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
  };
}
