import type {
  SemanticDocument,
  SemanticPackage,
  TenantOntology,
} from "~/composables/useOntologyCatalog";

export function useOntologyWorkspace() {
  const { t } = useI18n();
  const route = useRoute();
  const api = useOntologyCatalog();
  const packages = ref<SemanticPackage[]>([]),
    ontologies = ref<TenantOntology[]>([]);
  const selectedPackage = ref<SemanticPackage>();
  const document = ref<SemanticDocument>();
  const original = ref("");
  const saved = shallowRef<{
    document?: SemanticDocument;
    mappings: Record<string, string>;
    name: string;
    id: string;
    targetId: string;
    targetRevision?: string;
  }>();
  const canManage = ref(false);
  const id = ref(""),
    name = ref(""),
    revision = ref<string>();
  const targetId = ref(""),
    targetRevision = ref<string>();
  const target = ref<SemanticDocument>();
  const mappings = ref<Record<string, string>>({});
  const selected = ref(""),
    search = ref(""),
    tab = ref("overview");
  const busy = ref(false),
    error = ref(""),
    notice = ref(""),
    reviewing = ref(false);
  const pending = ref<SemanticDocument>();
  let epoch = 0;
  let operation = 0;
  const pipelineCatalog = usePipelineCatalog();
  const pipelines = ref<Array<{ pipeline_id: string; name: string }>>([]);
  const pipelineId = ref(""),
    blockId = ref(""),
    pipelineHead = ref("");
  const pipelineBlocks = ref<Array<{ value: string; label: string }>>([]);
  async function choosePipeline(value: string | undefined) {
    const request = epoch;
    pipelineId.value = value ?? "";
    blockId.value = "";
    pipelineBlocks.value = [];
    if (!value) return;
    await run(async () => {
      const artifact = await pipelineCatalog.read(value);
      if (request !== epoch || pipelineId.value !== value) return;
      pipelineHead.value = artifact.head_revision_id;
      pipelineBlocks.value = artifact.definition.pipeline
        .filter(
          (step) => !["forward", "filter", "pipeline"].includes(step.type),
        )
        .map((step) => ({
          value: step.step,
          label: artifact.definition.editor?.labels?.[step.step] ?? step.step,
        }));
    });
  }
  async function bind() {
    await run(async () => {
      await api.query(
        "mutation($ontology:String!,$pipeline:String!,$block:String!,$expected:String!){bindPluginOntology(ontologyId:$ontology,pipelineId:$pipeline,blockId:$block,expectedPipelineRevision:$expected)}",
        {
          ontology: id.value,
          pipeline: pipelineId.value,
          block: blockId.value,
          expected: pipelineHead.value,
        },
      );
      const current = await api.workspace(id.value);
      revision.value = current.revision_id;
      notice.value = t(
        "ontologyManager.ontology_linked_in_a_new_pipeline_draft_review_p",
      );
    });
  }
  function discard() {
    const snapshot = saved.value;
    if (!snapshot) return;
    document.value = snapshot.document
      ? structuredClone(toRaw(snapshot.document))
      : undefined;
    mappings.value = { ...snapshot.mappings };
    name.value = snapshot.name;
    id.value = snapshot.id;
    targetId.value = snapshot.targetId;
    targetRevision.value = snapshot.targetRevision;
    target.value = undefined;
    void run(async () => {
      const request = epoch;
      if (targetId.value && targetRevision.value) {
        const data = await api.document(targetId.value, targetRevision.value);
        if (request === epoch) target.value = data;
      }
    });
  }
  const { session } = useAccount();
  const resources = computed(() => document.value?.resources ?? []);
  const resource = computed(() =>
    resources.value.find((r) => r.resource_id === selected.value),
  );
  const types = computed(() =>
    resources.value.filter((r) => r.kind === "object_type"),
  );
  const properties = computed(() =>
    resources.value.filter(
      (r) => r.kind === "property" && r.owner_id === selected.value,
    ),
  );
  const links = computed(() =>
    resources.value.filter(
      (r) =>
        r.kind === "link_type" &&
        (r.attributes.source_type === selected.value ||
          r.attributes.target_type === selected.value),
    ),
  );
  function snapshot() {
    return JSON.stringify({
      document: document.value,
      mappings: mappings.value,
      name: name.value,
      id: id.value,
      targetId: targetId.value,
      targetRevision: targetRevision.value,
    });
  }
  const dirty = computed(() => original.value !== snapshot());
  const slots = computed(
    () =>
      selectedPackage.value?.ontologies
        .flatMap((o) => o.resources)
        .filter(
          (r) => r.kind === "link_type" && r.attributes.mapping_slot === true,
        ) ?? [],
  );
  const targetTypes = computed(
    () => target.value?.resources.filter((r) => r.kind === "object_type") ?? [],
  );
  const graphNodes = computed(() => {
    const ids = [
      ...new Set([
        selected.value,
        ...links.value.flatMap((l) => [
          String(l.attributes.source_type),
          String(l.attributes.target_type),
        ]),
      ]),
    ];
    return ids.filter(Boolean).map((id, i) => ({
      id,
      position: {
        x: i === 0 ? 260 : 20 + ((i - 1) % 2) * 480,
        y: i === 0 ? 120 : Math.floor((i - 1) / 2) * 110,
      },
      data: {
        label:
          resources.value.find((r) => r.resource_id === id)?.display_name ?? id,
      },
      style: {
        borderColor: "rgb(var(--ui-border-control))",
        background: "rgb(var(--ui-surface))",
        color: "rgb(var(--ui-foreground))",
      },
    }));
  });
  const graphEdges = computed(() =>
    links.value.map((l) => ({
      id: l.resource_id,
      source: String(l.attributes.source_type),
      target: String(l.attributes.target_type),
      label: l.display_name,
    })),
  );
  function checkpoint() {
    original.value = snapshot();
    saved.value = {
      document: document.value
        ? structuredClone(toRaw(document.value))
        : undefined,
      mappings: { ...mappings.value },
      name: name.value,
      id: id.value,
      targetId: targetId.value,
      targetRevision: targetRevision.value,
    };
  }
  async function refresh() {
    const request = epoch;
    const [p, o, availablePipelines, allowed] = await Promise.all([
      api.packages(),
      api.browse(),
      pipelineCatalog.browse(),
      api.canManage(),
    ]);
    if (request !== epoch) return;
    canManage.value = allowed;
    pipelines.value = availablePipelines;
    packages.value = p;
    ontologies.value = o;
    if (!document.value && typeof route.query.package === "string") {
      const requested = p.find((item) => item.id === route.query.package);
      if (requested) start(requested);
    }
    if (!document.value) checkpoint();
  }
  async function run(action: () => Promise<void>) {
    const request = ++operation;
    busy.value = true;
    error.value = "";
    try {
      await action();
    } catch {
      if (request !== operation) return;
      error.value = t(
        "ontologyManager.this_operation_could_not_be_completed_check_your",
      );
    } finally {
      if (request === operation) busy.value = false;
    }
  }
  function clear() {
    epoch++;
    operation++;
    busy.value = false;
    canManage.value = false;
    api.cancel();
    pipelineCatalog.cancel();
    pipelines.value = [];
    pipelineBlocks.value = [];
    packages.value = [];
    ontologies.value = [];
    document.value = undefined;
    target.value = undefined;
    selectedPackage.value = undefined;
    id.value = "";
    name.value = "";
    targetId.value = "";
    targetRevision.value = undefined;
    pending.value = undefined;
    reviewing.value = false;
    pipelineId.value = "";
    blockId.value = "";
    mappings.value = {};
    revision.value = undefined;
    error.value = "";
    notice.value = "";
    checkpoint();
  }
  onActivated(() => {
    void run(refresh);
  });
  onDeactivated(clear);
  watch([() => session.value?.tenant, () => session.value?.subject], () => {
    clear();
    void run(refresh);
  });
  function start(p: SemanticPackage) {
    epoch++;
    selectedPackage.value = p;
    id.value = `${p.plugin_id}-${p.id.split(".").at(-1)}-${crypto.randomUUID().slice(0, 8)}`;
    name.value = p.name;
    revision.value = undefined;
    targetId.value = "";
    targetRevision.value = undefined;
    target.value = undefined;
    mappings.value = {};
    document.value = {
      version: p.version,
      resources: p.ontologies
        .flatMap((o) => o.resources)
        .map((r) => structuredClone(toRaw(r))),
    };
    selected.value = types.value[0]?.resource_id ?? "";
    notice.value = t(
      "ontologyManager.plugin_original_changes_are_saved_only_for_your_",
    );
    checkpoint();
  }
  async function open(item: TenantOntology) {
    if (dirty.value) return;
    await run(async () => {
      const request = ++epoch;
      const workspace = await api.workspace(item.ontologyId);
      if (request !== epoch) return;
      id.value = item.ontologyId;
      name.value = item.displayName;
      revision.value = workspace.revision_id;
      document.value = workspace.ontology;
      mappings.value = { ...(workspace.ontology.participant_mappings ?? {}) };
      const origin = workspace.ontology.plugin_origin;
      selectedPackage.value = packages.value.find(
        (p) =>
          p.id === origin?.package_id && p.version === origin?.package_version,
      );
      targetId.value = workspace.ontology.target_origin?.ontology_id ?? "";
      targetRevision.value = workspace.ontology.target_origin?.revision_id;
      const pinnedTarget =
        targetId.value && targetRevision.value
          ? await api.document(targetId.value, targetRevision.value)
          : undefined;
      if (request !== epoch) return;
      target.value = pinnedTarget;
      selected.value = types.value[0]?.resource_id ?? "";
      notice.value =
        origin && !selectedPackage.value
          ? t("ontologyManager.differentVersion")
          : t("ontologyManager.tenantDraft");
      checkpoint();
    });
  }
  async function chooseTarget(value: string | undefined) {
    const previous = ++epoch;
    targetId.value = value ?? "";
    targetRevision.value = ontologies.value.find((o) => o.ontologyId === value)
      ?.productionPublication?.revisionId;
    mappings.value = {};
    target.value = undefined;
    if (targetId.value && targetRevision.value)
      await run(async () => {
        const data = await api.document(targetId.value, targetRevision.value!);
        if (previous === epoch) target.value = data;
      });
  }
  function setMapping(slot: string, value: string | undefined) {
    mappings.value = Object.fromEntries([
      ...Object.entries(mappings.value).filter(([key]) => key !== slot),
      ...(value ? [[slot, value]] : []),
    ]);
  }
  async function review() {
    await run(async () => {
      pending.value = selectedPackage.value
        ? await api.preview(
            selectedPackage.value.id,
            selectedPackage.value.version,
            targetId.value || undefined,
            targetRevision.value,
            mappings.value,
          )
        : document.value;
      reviewing.value = true;
    });
  }
  async function save() {
    await run(async () => {
      if (!document.value) return;
      revision.value = selectedPackage.value
        ? await api.save(
            id.value,
            name.value,
            selectedPackage.value.id,
            selectedPackage.value.version,
            targetId.value || undefined,
            targetRevision.value,
            mappings.value,
            revision.value,
          )
        : await api.saveDraft(
            id.value,
            name.value,
            document.value,
            revision.value,
          );
      if (pending.value) document.value = pending.value;
      checkpoint();
      reviewing.value = false;
      notice.value = t(
        "ontologyManager.draft_saved_for_this_organization_publish_it_whe",
      );
      await refresh();
    });
  }
  async function publish() {
    await run(async () => {
      if (!revision.value) return;
      await api.publish(id.value, name.value, revision.value);
      const workspace = await api.workspace(id.value);
      revision.value = workspace.revision_id;
      await refresh();
      notice.value = t(
        "ontologyManager.ontology_published_existing_deployed_pipelines_r",
      );
    });
  }
  function newOntology() {
    epoch++;
    selectedPackage.value = undefined;
    document.value = { version: "1", resources: [] };
    id.value = `ontology-${crypto.randomUUID().slice(0, 8)}`;
    name.value = "";
    revision.value = undefined;
    targetId.value = "";
    targetRevision.value = undefined;
    target.value = undefined;
    mappings.value = {};
    selected.value = "";
    checkpoint();
  }
  const newTypeName = ref(""),
    newTypeId = ref("");
  function addType() {
    if (!document.value || !canManage.value || !newTypeName.value.trim())
      return;
    const identifier = newTypeId.value.trim() || `type-${crypto.randomUUID()}`;
    if (resources.value.some((r) => r.resource_id === identifier)) {
      error.value = t("ontologyManager.duplicateIdentifier");
      return;
    }
    document.value.resources.push({
      resource_id: identifier,
      kind: "object_type",
      display_name: newTypeName.value,
      references: [],
      attributes: {},
    });
    selected.value = identifier;
    newTypeId.value = "";
    newTypeName.value = "";
  }
  const newPropertyName = ref(""),
    newPropertyId = ref(""),
    newPropertyType = ref("string");
  function addProperty() {
    if (
      !document.value ||
      !canManage.value ||
      !selected.value ||
      !newPropertyName.value.trim()
    )
      return;
    const identifier =
      newPropertyId.value.trim() || `${selected.value}.${crypto.randomUUID()}`;
    if (resources.value.some((r) => r.resource_id === identifier)) {
      error.value = t("ontologyManager.duplicateIdentifier");
      return;
    }
    document.value.resources.push({
      resource_id: identifier,
      kind: "property",
      display_name: newPropertyName.value,
      owner_id: selected.value,
      value_type: newPropertyType.value,
      references: [],
      attributes: {},
    });
    newPropertyName.value = "";
    newPropertyId.value = "";
  }
  return {
    canManage,
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
  };
}
