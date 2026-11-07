import { ontologyGraph } from "~/utils/ontology-graph";
import type {
  OntologyWorkspace,
  OntologyCommand,
  OntologyResource,
} from "~/types/ontology";
import type {
  SemanticDocument,
  SemanticPackage,
  TenantOntology,
} from "~/composables/useOntologyCatalog";
import {
  parseSemanticDocument,
  sameOntologyDefinition,
} from "~/utils/ontology";
import { withOntologyType } from "~/utils/ontology-edit";
import type { OntologyTypeDraft } from "~/utils/ontology-edit";

export function useOntologyWorkspace(ontologyId?: string) {
  const { t } = useI18n();
  const officialLabels = useOfficialOntologyLabels();
  const route = useRoute();
  const api = useOntologyCatalog();
  const packages = ref<SemanticPackage[]>([]),
    ontologies = ref<TenantOntology[]>([]);
  const artifact = shallowRef<OntologyWorkspace>();
  const branch = ref("main");
  const deleted = computed(
    () => artifact.value?.authoring.deleted_at_ms != null,
  );
  const canDelete = ref(false);
  const selectedPackage = ref<SemanticPackage>();
  const document = ref<SemanticDocument>();
  const publishedDocument = shallowRef<SemanticDocument>();
  const published = computed(() =>
    sameOntologyDefinition(document.value, publishedDocument.value),
  );
  let publicationKey = "";
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
  const canCreate = ref(false),
    canPublish = ref(false);
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
  const expandedPackages = ref(new Set<string>());
  watch([id, revision], () => expandedPackages.value.clear());
  function toggleLayer(packageId: string) {
    const selectedLayer = document.value?.resources.find(
      (resource) => resource.resource_id === selected.value,
    )?.attributes.plugin_package_id;
    const expanded =
      expandedPackages.value.has(packageId) || selectedLayer === packageId;
    selected.value = "";
    if (expanded) expandedPackages.value.delete(packageId);
    else expandedPackages.value.add(packageId);
  }
  const busy = ref(false),
    error = ref(""),
    notice = ref(""),
    reviewing = ref(false);
  const pending = ref<SemanticDocument>();
  let epoch = 0;
  let operation = 0;
  function checkout(value: string) {
    if (busy.value || dirty.value || !artifact.value) return;
    branch.value = value;
    applyWorkspace(artifact.value);
    void run(refreshTarget);
  }
  function applyWorkspace(value: OntologyWorkspace, preserveSelection = false) {
    artifact.value = value;
    revision.value = value.revision_id;
    const state =
      branch.value === "main"
        ? undefined
        : value.authoring.branches[branch.value];
    if (!state) branch.value = "main";
    const model = state?.definition ?? value.ontology;
    document.value = parseSemanticDocument(JSON.stringify(model));
    name.value = state?.display_name ?? value.display_name ?? name.value;
    mappings.value = { ...(model.participant_mappings ?? {}) };
    const origin = model.plugin_origin;
    selectedPackage.value = packages.value.find(
      (p) =>
        p.id === origin?.package_id && p.version === origin?.package_version,
    );
    if (
      targetId.value !== (model.target_origin?.ontology_id ?? "") ||
      targetRevision.value !== model.target_origin?.revision_id
    )
      target.value = undefined;
    targetId.value = model.target_origin?.ontology_id ?? "";
    targetRevision.value = model.target_origin?.revision_id;
    if (
      !preserveSelection ||
      !model.resources.some((r) => r.resource_id === selected.value)
    )
      selected.value = "";
    checkpoint();
  }
  async function author(command: OntologyCommand) {
    if (busy.value || dirty.value || !revision.value) return;
    return await run(async () => {
      const request = epoch;
      const expected = revision.value;
      if (!expected) return;
      const value = await api.author(id.value, expected, command);
      if (request !== epoch) return;
      if (command.operation === "create_branch") branch.value = command.branch;
      if (
        command.operation === "delete_branch" &&
        command.branch === branch.value
      )
        branch.value =
          artifact.value?.authoring.branches[command.branch]?.parent_branch ??
          "main";
      applyWorkspace(value);
      await refreshTarget();
      await refresh();
    });
  }
  function discard() {
    const snapshot = saved.value;
    if (!snapshot) return;
    document.value = snapshot.document
      ? parseSemanticDocument(JSON.stringify(snapshot.document))
      : undefined;
    mappings.value = { ...snapshot.mappings };
    name.value = snapshot.name;
    id.value = snapshot.id;
    targetId.value = snapshot.targetId;
    targetRevision.value = snapshot.targetRevision;
    target.value = undefined;
    void run(refreshTarget);
  }
  async function refreshTarget() {
    const request = epoch;
    const targetOntology = targetId.value;
    const pin = targetRevision.value;
    if (!targetOntology || !pin || target.value) return;
    const data = await api.document(targetOntology, pin);
    if (
      request === epoch &&
      targetId.value === targetOntology &&
      targetRevision.value === pin
    )
      target.value = data;
  }
  const { session } = useAccount();
  const resources = computed(() => document.value?.resources ?? []);
  const resource = computed(() =>
    resources.value.find((r) => r.resource_id === selected.value),
  );
  const types = computed(() =>
    resources.value.filter((r) =>
      ["object_type", "event_type"].includes(r.kind),
    ),
  );
  const properties = computed(() =>
    resources.value.filter(
      (r) =>
        r.kind === "property" &&
        (!selected.value || r.owner_id === selected.value),
    ),
  );
  const links = computed(() =>
    resources.value.filter(
      (r) =>
        r.kind === "link_type" &&
        (!selected.value ||
          r.attributes.source_type === selected.value ||
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
  function resourceLabel(resource: OntologyResource) {
    return officialLabels.resourceLabel(
      resource,
      document.value?.plugin_origin?.package_id ?? selectedPackage.value?.id,
      !artifact.value && !!selectedPackage.value,
    );
  }
  const displayTitle = computed(() => {
    const source = selectedPackage.value;
    return source && name.value === source.name
      ? officialLabels.packageLabel(source.id, name.value)
      : name.value;
  });
  const graph = computed(() =>
    ontologyGraph(
      document.value,
      selected.value,
      resourceLabel,
      expandedPackages.value,
      officialLabels.packageLabel,
    ),
  );
  const graphNodes = computed(() => graph.value.nodes);
  const graphEdges = computed(() => graph.value.edges);
  function checkpoint() {
    original.value = snapshot();
    saved.value = {
      document: document.value
        ? parseSemanticDocument(JSON.stringify(document.value))
        : undefined,
      mappings: { ...mappings.value },
      name: name.value,
      id: id.value,
      targetId: targetId.value,
      targetRevision: targetRevision.value,
    };
  }
  async function refreshPublication() {
    const request = epoch;
    const ontology = id.value;
    const pin = ontologies.value.find((item) => item.ontologyId === ontology)
      ?.productionPublication?.revisionId;
    if (!pin) {
      publicationKey = "";
      publishedDocument.value = undefined;
      return;
    }
    const key = `${ontology}/${pin}`;
    if (key === publicationKey) return;
    const value = await api.document(ontology, pin);
    if (request !== epoch || ontology !== id.value) return;
    publishedDocument.value = value;
    publicationKey = key;
  }
  async function refresh() {
    const request = epoch;
    const [p, o, allowed] = await Promise.all([
      api.packages().catch(() => []),
      api.browse(),
      api.canManage().catch(() => false),
    ]);
    if (request !== epoch) return;
    canManage.value = allowed;
    canCreate.value = allowed;
    if (revision.value) {
      const [editable, publishable, deletable] = await Promise.all([
        api.canManage(id.value),
        api.canManage(id.value, "PUBLISH"),
        api.canManage(id.value, "DELETE").catch(() => false),
      ]);
      if (request !== epoch) return;
      canManage.value = editable;
      canPublish.value = publishable;
      canDelete.value = deletable;
    }
    packages.value = p;
    ontologies.value = o;
    if (!document.value && typeof route.query.package === "string") {
      const requested = p.find((item) => item.id === route.query.package);
      if (requested) start(requested);
    }
    if (!document.value && ontologyId) {
      const requested = o.find((item) => item.ontologyId === ontologyId);
      checkpoint();
      await open(
        requested ?? {
          ontologyId,
          displayName: ontologyId,
          productionPublication: null,
        },
      );
    } else if (
      !document.value &&
      route.path === "/ontology/new" &&
      !route.query.package
    ) {
      newOntology();
    }
    if (!document.value) checkpoint();
    await refreshPublication();
  }
  async function run(action: () => Promise<void>) {
    const request = ++operation;
    busy.value = true;
    error.value = "";
    try {
      await action();
      return true;
    } catch {
      if (request !== operation) return;
      error.value = t(
        "ontologyManager.this_operation_could_not_be_completed_check_your",
      );
      return false;
    } finally {
      if (request === operation) busy.value = false;
    }
  }
  function clear() {
    epoch++;
    operation++;
    busy.value = false;
    canManage.value = false;
    canCreate.value = false;
    canPublish.value = false;
    canDelete.value = false;
    artifact.value = undefined;
    branch.value = "main";
    newPropertyName.value = "";
    newPropertyId.value = "";
    api.cancel();
    packages.value = [];
    ontologies.value = [];
    document.value = undefined;
    publishedDocument.value = undefined;
    publicationKey = "";
    target.value = undefined;
    selectedPackage.value = undefined;
    id.value = "";
    name.value = "";
    targetId.value = "";
    targetRevision.value = undefined;
    pending.value = undefined;
    reviewing.value = false;
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
    publishedDocument.value = undefined;
    publicationKey = "";
    canManage.value = canCreate.value;
    canPublish.value = false;
    artifact.value = undefined;
    branch.value = "main";
    canDelete.value = false;
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
    selected.value = "";
    notice.value = t(
      "ontologyManager.plugin_original_changes_are_saved_only_for_your_",
    );
    checkpoint();
  }
  async function open(item: TenantOntology) {
    if (dirty.value) return;
    await run(async () => {
      const request = ++epoch;
      const [workspace, editable, publishable, deletable] = await Promise.all([
        api.workspace(item.ontologyId),
        api.canManage(item.ontologyId),
        api.canManage(item.ontologyId, "PUBLISH"),
        api.canManage(item.ontologyId, "DELETE").catch(() => false),
      ]);
      if (request !== epoch) return;
      canManage.value = editable;
      canPublish.value = publishable;
      id.value = item.ontologyId;
      name.value = item.displayName;
      canDelete.value = deletable;
      branch.value = "main";
      applyWorkspace(workspace);
      const origin = workspace.ontology.plugin_origin;
      await refreshTarget();
      if (request !== epoch) return;
      selected.value = "";
      await refreshPublication();
      notice.value =
        origin && !selectedPackage.value
          ? t("ontologyManager.differentVersion")
          : t("ontologyManager.tenantDraft");
      checkpoint();
    });
  }
  async function chooseTarget(value: string | undefined) {
    epoch++;
    targetId.value = value ?? "";
    targetRevision.value = ontologies.value.find((o) => o.ontologyId === value)
      ?.productionPublication?.revisionId;
    mappings.value = {};
    target.value = undefined;
    await run(refreshTarget);
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
            branch.value,
          )
        : await api.saveDraft(
            id.value,
            name.value,
            document.value,
            revision.value,
            branch.value,
          );
      const workspace = await api.workspace(id.value);
      applyWorkspace(workspace, true);
      await refreshTarget();
      reviewing.value = false;
      notice.value = t(
        "ontologyManager.draft_saved_for_this_organization_publish_it_whe",
      );
      await refresh();
    });
  }
  async function publish() {
    await run(async () => {
      if (!revision.value || branch.value !== "main" || deleted.value) return;
      await api.publish(id.value, name.value, revision.value);
      const workspace = await api.workspace(id.value);
      applyWorkspace(workspace, true);
      await refresh();
      notice.value = t(
        "ontologyManager.ontology_published_existing_deployed_pipelines_r",
      );
    });
  }
  function newOntology() {
    epoch++;
    publishedDocument.value = undefined;
    publicationKey = "";
    canManage.value = canCreate.value;
    canPublish.value = false;
    artifact.value = undefined;
    branch.value = "main";
    canDelete.value = false;
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
  function addType(input: OntologyTypeDraft): boolean {
    if (!document.value || !canManage.value) return false;
    try {
      document.value = withOntologyType(document.value, input);
      selected.value = input.id;
      return true;
    } catch {
      error.value = t("ontologyManager.objectWizard.invalid");
      return false;
    }
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
    toggleLayer,
    artifact,
    branch,
    deleted,
    canDelete,
    checkout,
    author,
    published,
    canManage,
    canCreate,
    canPublish,
    packages,
    ontologies,
    selectedPackage,
    document,
    id,
    name,
    displayTitle,
    resourceLabel,
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
  };
}
