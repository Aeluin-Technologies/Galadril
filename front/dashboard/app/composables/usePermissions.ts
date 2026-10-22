import {
  ArrowLeftIcon,
  ArrowPathIcon,
  ArrowRightIcon,
  ClockIcon,
  UserGroupIcon,
  ArrowDownTrayIcon,
} from "@heroicons/vue/24/outline";
import { useDebounceFn } from "@vueuse/core";
import {
  accessGraphLayout,
  administrationError,
  generalAccessDraft,
  permissionBlocks,
  permissionImportInput,
  resourceAccessOptions,
  canSetResourceAccess,
  accessRuleIdentifier,
  roleInclusion,
  roleSyncDefaults,
} from "~/utils/administration.mjs";
export type Person = {
  userId: string;
  displayName?: string | null;
  email?: string | null;
  isActive: boolean;
};
export type Role = { roleName: string; displayName?: string | null };
export type Policy = { id: string; content: string; active: boolean };
export type AccessNode = {
  id: string;
  label: string;
  data: { kind: string; resourceId: string };
};
export type AccessEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
  data: { relations: string[] };
};
export type SyncConfiguration = {
  connectionId: string;
  claim: string;
  enabled: boolean;
  mappings: { externalGroup: string; roleName: string }[];
};
export interface DirectoryPage<T> {
  items: T[];
  after: string | null;
}
export type PermissionProps = {
  task?: "create-role";
  settings: {
    permission_import: boolean;
    connections: { domain: string; enabled: boolean; connection_id?: string }[];
  };
  users: Person[];
  roles: Role[];
  request: <T>(
    query: string,
    variables?: Record<string, unknown>,
  ) => Promise<T>;
};
export interface PermissionEvents {
  success: (message: string) => void;
  failure: (message: string) => void;
  settingsChanged: () => void;
  peopleChanged: () => void;
  managePerson: (person: Person) => void;
}
export function usePermissions(
  props: PermissionProps,
  events: PermissionEvents,
) {
  const { t, te } = useI18n();
  function formatFailure(error: unknown, fallback: string) {
    return administrationError(
      error,
      fallback,
      t("administration.service_version_mismatch"),
    );
  }

  const mode = ref<"" | "sync" | "roles" | "rules">("");
  const editorOpen = ref(false);
  const newRoleDetails = ref<HTMLDetailsElement>();
  const newRoleInput = ref<{ focus: () => void }>();
  watch(
    () => props.task,
    async (task) => {
      if (task !== "create-role") return;
      mode.value = "roles";
      editorOpen.value = true;
      await nextTick();
      if (newRoleDetails.value) newRoleDetails.value.open = true;
      newRoleInput.value?.focus();
      await choose("roles");
    },
    { immediate: true, flush: "post" },
  );
  const busy = ref(false);
  const search = ref("");
  const graph = ref<ReturnType<typeof accessGraphLayout>>({
    nodes: [],
    edges: [],
    truncated: false,
  });
  const selectedNode = ref<AccessNode>();
  const displayGraph = computed(() => ({
    ...graph.value,
    edges: graph.value.edges.map((edge: AccessEdge) => ({
      ...edge,
      label:
        edge.data.relations.length > 1
          ? t("administration.connection_count", {
              count: edge.data.relations.length,
            })
          : relationshipLabel(edge.data.relations[0] ?? ""),
    })),
  }));
  function relationshipLabel(relation: string) {
    const key = `administration.relations.${relation}`;
    return te(key) ? t(key) : t("administration.connected_to");
  }
  function selectAccessNode(id: string) {
    selectedNode.value = graph.value.nodes.find(
      (candidate: AccessNode) => candidate.id === id,
    );
    const node = selectedNode.value;
    if (!node) return;
    editorOpen.value = true;
    if (node.data.kind === "user" || node.data.kind === "role") {
      subject.value = `${node.data.kind}:${node.data.resourceId}`;
      selectedRole.value = "";
      parentRole.value = "";
    } else if (resourceAccessOptions(node.data.kind).length) {
      targetResource.value = node.id;
    }
  }
  function selectAccessEdge(id: string) {
    const edge = graph.value.edges.find((candidate) => candidate.id === id);
    if (!edge) return;
    selectAccessNode(edge.source);
    const target = graph.value.nodes.find(
      (node: AccessNode) => node.id === edge.target,
    );
    if (!target) return;
    if (target.data.kind === "role") {
      if (subject.value.startsWith("user:"))
        selectedRole.value = target.data.resourceId;
      else parentRole.value = target.data.resourceId;
    } else if (resourceAccessOptions(target.data.kind).length) {
      targetResource.value = target.id;
      nextTick(() => {
        access.value =
          edge.data.relations.find((relation: string) =>
            canSetResourceAccess(target.data.kind, relation),
          ) ??
          resourcePermissions.value[0]?.value ??
          "";
      });
    }
  }
  async function editSelectedRules() {
    editorOpen.value = false;
    ruleScope.value =
      selectedNode.value?.data.kind === "role"
        ? selectedNode.value.data.resourceId
        : "";
    await choose("rules");
  }
  function manageSelectedPerson() {
    const node = selectedNode.value;
    if (node?.data.kind !== "user") return;
    const person = people.value.find(
      (candidate) => candidate.userId === node.data.resourceId,
    );
    if (person) {
      editorOpen.value = false;
      awaitPersonEditor(person);
    }
  }
  async function renameSelected(name: string) {
    const node = selectedNode.value;
    if (!node || !["user", "role"].includes(node.data.kind)) return;
    await perform(
      async () => {
        await props.request(
          "mutation($kind: String!, $id: String!, $name: String!) { administration { setDirectoryDisplayName(subjectType: $kind, subjectId: $id, displayName: $name) } }",
          { kind: node.data.kind, id: node.data.resourceId, name },
        );
        await loadGraph();
        events.peopleChanged();
      },
      t("administration.display_name_saved"),
      t("administration.display_name_failed"),
    );
  }
  async function awaitPersonEditor(person: Person) {
    await nextTick();
    events.managePerson(person);
  }
  const people = ref<Person[]>(props.users);
  const roleOptions = ref<Role[]>(props.roles);
  const peopleAfter = ref<string | null>(null);
  const rolesAfter = ref<string | null>(null);
  const ruleRolesAfter = ref<string | null>(null);
  let directoryEpoch = 0;
  let ruleEpoch = 0;
  const newRole = ref("");
  const subject = ref("");
  const selectedRole = ref("");
  const parentRole = ref("");
  const targetResource = ref("");
  const access = ref("");
  const ruleStep = ref(1);
  const ruleMode = ref("blocks");
  const ruleScope = ref("");
  const roleSearch = ref("");
  const ruleRoles = ref<Role[]>(props.roles);
  const start = ref("08:00");
  const end = ref("18:00");
  const ruleDraft = ref<ReturnType<typeof generalAccessDraft>>();
  const ruleName = ref(t("administration.office_hours"));
  const ruleId = ref("");
  const policies = ref<Policy[]>([]);
  const policyId = ref("custom-access");
  const policyContent = ref("");
  const policyActive = ref(true);
  const manifest = ref("");
  const reviewImport = ref(false);
  const confirmImportSettings = ref(false);
  const synchronization = ref<SyncConfiguration>(roleSyncDefaults());
  const syncConfigurations = ref<SyncConfiguration[]>([]);
  const syncStep = ref(1);
  const choices = computed(
    () =>
      [
        {
          id: "sync",
          title: t("administration.copy.import_permissions_automatically"),
          description: t(
            "administration.copy.connect_company_groups_to_galadril_roles_new_sign",
          ),
          icon: ArrowDownTrayIcon,
        },
        {
          id: "roles",
          title: t("administration.copy.manage_roles_and_people"),
          description: t(
            "administration.copy.create_roles_give_people_access_and_explore_how",
          ),
          icon: UserGroupIcon,
        },
        {
          id: "rules",
          title: t("administration.copy.set_general_access_rules"),
          description: t(
            "administration.copy.choose_when_people_can_use_their_access_with",
          ),
          icon: ClockIcon,
        },
      ] as const,
  );
  const resources = computed<AccessNode[]>(() =>
    graph.value.nodes.filter(
      (node: AccessNode) => resourceAccessOptions(node.data.kind).length,
    ),
  );
  const selectedResource = computed(() =>
    resources.value.find(
      (node: AccessNode) => node.id === targetResource.value,
    ),
  );
  const resourcePermissions = computed<{ label: string; value: string }[]>(() =>
    resourceAccessOptions(selectedResource.value?.data.kind).map(
      (option: { label: string; value: string }) => ({
        ...option,
        label: t(`administration.permissions.${option.value}`),
      }),
    ),
  );
  const resourceAccessReady = computed(() =>
    canSetResourceAccess(selectedResource.value?.data.kind, access.value),
  );
  watch(selectedResource, () => {
    access.value = resourcePermissions.value[0]?.value ?? "";
  });
  const resourceLabels = computed<Record<string, string>>(() => ({
    user: t("administration.copy.person"),
    role: t("storage.permissions.role"),
    group: t("administration.copy.data_space"),
    project: t("administration.copy.project"),
    table: t("administration.copy.table"),
    raw: t("administration.copy.source_data"),
    document: t("administration.copy.document"),
    ontology: t("administration.copy.data_model"),
    pipeline: t("administration.copy.data_pipeline"),
    conversation: t("administration.copy.conversation"),
    entity_state: t("administration.copy.data_record"),
    event: t("command_palette.item_types.event"),
  }));
  const resourceLabel = (kind: string) =>
    resourceLabels.value[kind] || t("administration.resource");
  const scopedRoles = computed(() =>
    ruleScope.value &&
    !ruleRoles.value.some((role) => role.roleName === ruleScope.value)
      ? [{ roleName: ruleScope.value }, ...ruleRoles.value]
      : ruleRoles.value,
  );
  const relatedEdges = computed(() =>
    displayGraph.value.edges.filter(
      (edge: { source: string; target: string }) =>
        edge.source === selectedNode.value?.id ||
        edge.target === selectedNode.value?.id,
    ),
  );
  const displayName = (person: Person) =>
    person.displayName || person.email || person.userId;
  function nodeLabel(id: string) {
    return (
      graph.value.nodes.find((node: AccessNode) => node.id === id)?.label ||
      t("administration.unavailable_resource")
    );
  }
  async function perform(
    action: () => Promise<void>,
    success: string,
    fallback: string,
  ) {
    if (busy.value) return false;
    busy.value = true;
    try {
      await action();
      if (success) events.success(success);
      return true;
    } catch (error) {
      events.failure(formatFailure(error, fallback));
      return false;
    } finally {
      busy.value = false;
    }
  }
  async function loadGraph() {
    const epoch = ++directoryEpoch;
    const term = search.value;
    const data = await props.request<{
      accessGraph: { nodes: unknown[]; edges: unknown[]; truncated: boolean };
      users: DirectoryPage<Person>;
      roles: DirectoryPage<Role>;
    }>(
      "query($search: String) { administration { accessGraph(search: $search, limit: 100) { nodes { id label kind resourceId } edges { source target relation } truncated } users(search: $search, limit: 20) { items { userId displayName email isActive } after } roles(search: $search, limit: 20) { items { roleName displayName } after } } }",
      { search: term || null },
    );
    if (epoch !== directoryEpoch || term !== search.value) return;
    graph.value = accessGraphLayout(data.accessGraph);
    selectedNode.value = graph.value.nodes.find(
      (node: AccessNode) => node.id === selectedNode.value?.id,
    );
    people.value = data.users.items;
    peopleAfter.value = data.users.after;
    roleOptions.value = data.roles.items;
    rolesAfter.value = data.roles.after;
    if (
      selectedNode.value &&
      !graph.value.nodes.some(
        (node: AccessNode) => node.id === selectedNode.value?.id,
      )
    )
      selectedNode.value = undefined;
  }
  async function moreDirectory(kind: "users" | "roles") {
    const epoch = directoryEpoch;
    const term = search.value;
    const after = kind === "users" ? peopleAfter.value : rolesAfter.value;
    if (!after) return;
    const document =
      kind === "users"
        ? "query($search: String, $after: String) { administration { users(search: $search, after: $after, limit: 20) { items { userId displayName email isActive } after } } }"
        : "query($search: String, $after: String) { administration { roles(search: $search, after: $after, limit: 20) { items { roleName displayName } after } } }";
    const data = await props.request<{
      users?: DirectoryPage<Person>;
      roles?: DirectoryPage<Role>;
    }>(document, { search: term || null, after });
    if (epoch !== directoryEpoch || term !== search.value) return;
    if (data.users) {
      people.value.push(...data.users.items);
      peopleAfter.value = data.users.after;
    }
    if (data.roles) {
      roleOptions.value.push(...data.roles.items);
      rolesAfter.value = data.roles.after;
    }
  }
  async function loadRuleRoles(append = false) {
    const epoch = ++ruleEpoch;
    const term = roleSearch.value;
    const after = append ? ruleRolesAfter.value : null;
    if (!append) {
      ruleRoles.value = [];
      ruleRolesAfter.value = null;
    }
    const data = await props.request<{ roles: DirectoryPage<Role> }>(
      "query($search: String, $after: String) { administration { roles(search: $search, after: $after, limit: 20) { items { roleName displayName } after } } }",
      { search: term || null, after },
    );
    if (epoch !== ruleEpoch || term !== roleSearch.value) return;
    ruleRoles.value = append
      ? [...ruleRoles.value, ...data.roles.items]
      : data.roles.items;
    ruleRolesAfter.value = data.roles.after;
  }
  async function loadPolicies() {
    const data = await props.request<{ cedarPolicies: Policy[] }>(
      "query { administration { cedarPolicies { id content active } } }",
    );
    policies.value = data.cedarPolicies;
  }
  async function choose(next: typeof mode.value) {
    mode.value = next;
    if (next === "roles")
      await perform(
        loadGraph,
        "",
        t("administration.copy.the_access_map_could_not_be_loaded_use"),
      );
    if (next === "rules")
      await perform(
        async () => {
          await loadPolicies();
          await loadRuleRoles();
        },
        "",
        t("administration.copy.your_saved_access_rules_could_not_be_loaded"),
      );
    if (next === "sync")
      await perform(
        async () => {
          const data = await props.request<{
            roleSyncConfigurations: SyncConfiguration[];
            roles: DirectoryPage<Role>;
            users: DirectoryPage<Person>;
          }>(
            "query { administration { roleSyncConfigurations { connectionId claim enabled mappings { externalGroup roleName } } users(limit: 20) { items { userId displayName email isActive } after } roles(limit: 20) { items { roleName displayName } after } } }",
          );
          syncConfigurations.value = data.roleSyncConfigurations;
          ruleRoles.value = data.roles.items;
          ruleRolesAfter.value = data.roles.after;
          people.value = data.users.items;
          peopleAfter.value = data.users.after;
        },
        "",
        t(
          "administration.copy.company_role_synchronization_could_not_be_loaded_check",
        ),
      );
  }
  watch(
    search,
    useDebounceFn(() => {
      if (mode.value === "roles")
        return loadGraph().catch((error) =>
          events.failure(
            formatFailure(
              error,
              t(
                "administration.copy.your_search_could_not_be_completed_use_reload",
              ),
            ),
          ),
        );
    }, 250),
  );
  watch(
    roleSearch,
    useDebounceFn(
      () =>
        loadRuleRoles().catch((error) =>
          events.failure(
            formatFailure(
              error,
              t(
                "administration.copy.roles_could_not_be_searched_clear_the_search",
              ),
            ),
          ),
        ),
      250,
    ),
  );
  function selectConnection() {
    const saved = syncConfigurations.value.find(
      (item) => item.connectionId === synchronization.value.connectionId,
    );
    synchronization.value = saved
      ? structuredClone(saved)
      : roleSyncDefaults(synchronization.value.connectionId);
  }
  async function createRole() {
    await perform(
      async () => {
        await props.request(
          "mutation($name: String!) { administration { createRole(roleName: $name) } }",
          { name: newRole.value.trim() },
        );
        newRole.value = "";
        await loadGraph();
        events.peopleChanged();
      },
      t("administration.copy.role_created_choose_who_should_receive_it_below"),
      t("administration.copy.this_role_could_not_be_created_use_a"),
    );
  }
  async function assignRole(enabled = true) {
    await perform(
      async () => {
        await props.request(
          enabled
            ? "mutation($user: String!, $role: String!) { administration { assignRoleToUser(userId: $user, roleName: $role) } }"
            : "mutation($user: String!, $role: String!) { administration { unassignRoleFromUser(userId: $user, roleName: $role) } }",
          { user: subject.value.slice(5), role: selectedRole.value },
        );
        await loadGraph();
        events.peopleChanged();
      },
      enabled
        ? t("administration.copy.role_assigned_the_access_map_is_up_to")
        : t("administration.copy.role_removed_the_access_map_is_up_to"),
      t("administration.copy.this_role_membership_could_not_be_changed_check"),
    );
  }
  async function includeRole(enabled = true) {
    await perform(
      async () => {
        await props.request(
          "mutation($role: String!, $parent: String!, $enabled: Boolean!) { administration { setRoleParent(roleName: $role, parentRoleName: $parent, enabled: $enabled) } }",
          roleInclusion(selectedRole.value, parentRole.value, enabled),
        );
        await loadGraph();
      },
      t("administration.copy.role_inheritance_updated"),
      t("administration.copy.this_role_connection_could_not_be_saved_choose"),
    );
  }
  async function saveResourceAccess(enabled = true) {
    const node = resources.value.find(
      (item: AccessNode) => item.id === targetResource.value,
    );
    const separator = subject.value.indexOf(":");
    if (
      !node ||
      separator < 0 ||
      !canSetResourceAccess(node.data.kind, access.value)
    )
      return;
    await perform(
      async () => {
        await props.request(
          "mutation($type: String!, $resource: String!, $subjectType: String!, $subject: String!, $relation: String!, $enabled: Boolean!) { administration { setResourceAccess(resourceType: $type, resourceId: $resource, subjectType: $subjectType, subjectId: $subject, relation: $relation, enabled: $enabled) } }",
          {
            type: node.data.kind,
            resource: node.data.resourceId,
            subjectType: subject.value.slice(0, separator),
            subject: subject.value.slice(separator + 1),
            relation: access.value,
            enabled,
          },
        );
        await loadGraph();
      },
      enabled
        ? t("administration.copy.data_access_granted_the_access_map_is_up")
        : t("administration.copy.data_access_removed_the_access_map_is_up"),
      t("administration.copy.this_access_change_could_not_be_saved_select"),
    );
  }
  function reviewRule() {
    try {
      ruleId.value = accessRuleIdentifier(ruleName.value);
      ruleDraft.value = generalAccessDraft({
        roles: ruleScope.value ? [ruleScope.value] : [],
        start: start.value,
        end: end.value,
      });
      ruleStep.value = 3;
    } catch (error) {
      events.failure(
        error instanceof Error
          ? error.message
          : t("administration.copy.review_the_access_hours_before_continuing"),
      );
    }
  }
  async function saveRule() {
    if (!ruleDraft.value) return;
    await perform(
      async () => {
        await props.request(
          "mutation($id: String!, $configuration: GeneralAccessInput!, $active: Boolean!) { administration { configureGeneralAccess(id: $id, configuration: $configuration, active: $active) } }",
          {
            id: ruleId.value,
            configuration: ruleDraft.value,
            active: true,
          },
        );
        await loadPolicies();
        ruleStep.value = 1;
      },
      t(
        "administration.copy.general_access_rule_saved_administrators_keep_access_to",
      ),
      t("administration.copy.this_access_rule_could_not_be_saved_check"),
    );
  }
  async function savePolicy(
    policy: Policy = {
      id: policyId.value,
      content: policyContent.value,
      active: policyActive.value,
    },
  ) {
    await perform(
      async () => {
        await props.request(
          "mutation($id: String!, $content: String!, $active: Boolean!) { administration { setCedarPolicy(policyId: $id, content: $content, isActive: $active) } }",
          policy,
        );
        await loadPolicies();
      },
      t(
        "administration.copy.access_policy_saved_administrators_keep_access_to_manage",
      ),
      t("administration.copy.this_cedar_policy_could_not_be_saved_check"),
    );
  }
  function editPolicy(policy: Policy) {
    policyId.value = policy.id;
    policyContent.value = policy.content;
    policyActive.value = policy.active;
    ruleMode.value = "code";
  }
  async function toggleImports() {
    await perform(
      async () => {
        await props.request(
          "mutation($enabled: Boolean!) { administration { setPermissionImport(enabled: $enabled) } }",
          { enabled: !props.settings.permission_import },
        );
        events.settingsChanged();
      },
      props.settings.permission_import
        ? t(
            "administration.copy.permission_import_disabled_existing_access_stays_in_place",
          )
        : t(
            "administration.copy.permission_import_enabled_for_your_organization",
          ),
      t(
        "administration.copy.permission_import_settings_could_not_be_saved_check",
      ),
    );
  }
  async function saveSynchronization() {
    if (
      !synchronization.value.connectionId ||
      !synchronization.value.mappings.length
    )
      return;
    await perform(
      async () => {
        await props.request(
          "mutation($configuration: RoleSyncConfigurationInput!) { administration { configureRoleSync(configuration: $configuration) } }",
          { configuration: synchronization.value },
        );
        syncStep.value = 1;
      },
      t(
        "administration.copy.company_role_synchronization_saved_mapped_access_is_reconciled",
      ),
      t("administration.copy.company_roles_could_not_be_synchronized_choose_a"),
    );
  }
  function reviewPermissions() {
    try {
      permissionBlocks(manifest.value);
      reviewImport.value = true;
    } catch {
      events.failure(
        t(
          "administration.copy.complete_each_permission_change_or_correct_the_configuration",
        ),
      );
    }
  }
  async function applyPermissions() {
    await perform(
      async () => {
        const data = await props.request<{
          importPermissions: {
            applied: number;
            complete: boolean;
            failedAt: number | null;
          };
        }>(
          "mutation($manifest: PermissionManifestInput!) { administration { importPermissions(manifest: $manifest) { applied complete failedAt } } }",
          { manifest: permissionImportInput(JSON.parse(manifest.value)) },
        );
        const result = data.importPermissions;
        reviewImport.value = false;
        events.peopleChanged();
        if (!result.complete) {
          events.failure(
            t(
              "administration.copy.value0_changes_applied_change_value1_could_not_be",
              { value0: result.applied, value1: (result.failedAt ?? 0) + 1 },
            ),
          );
        } else
          events.success(
            t("administration.copy.value0_access_changes_applied", {
              value0: result.applied,
            }),
          );
      },
      "",
      t(
        "administration.copy.the_permission_configuration_could_not_be_imported_review",
      ),
    );
  }
  return {
    mode,
    editorOpen,
    newRoleDetails,
    newRoleInput,
    busy,
    search,
    graph: displayGraph,
    relationshipLabel,
    selectedNode,
    selectAccessNode,
    selectAccessEdge,
    editSelectedRules,
    manageSelectedPerson,
    renameSelected,
    people,
    roleOptions,
    peopleAfter,
    rolesAfter,
    ruleRolesAfter,
    directoryEpoch,
    ruleEpoch,
    newRole,
    subject,
    selectedRole,
    parentRole,
    targetResource,
    access,
    ruleStep,
    ruleMode,
    ruleScope,
    roleSearch,
    ruleRoles,
    start,
    end,
    ruleDraft,
    ruleName,
    ruleId,
    policies,
    policyId,
    policyContent,
    policyActive,
    manifest,
    reviewImport,
    confirmImportSettings,
    synchronization,
    syncConfigurations,
    syncStep,
    choices,
    resources,
    selectedResource,
    resourcePermissions,
    resourceAccessReady,
    resourceLabels,
    resourceLabel,
    scopedRoles,
    relatedEdges,
    displayName,
    nodeLabel,
    perform,
    loadGraph,
    moreDirectory,
    loadRuleRoles,
    loadPolicies,
    choose,
    selectConnection,
    createRole,
    assignRole,
    includeRole,
    saveResourceAccess,
    reviewRule,
    saveRule,
    savePolicy,
    editPolicy,
    toggleImports,
    saveSynchronization,
    reviewPermissions,
    applyPermissions,
    settings: computed(() => props.settings),
    users: computed(() => props.users),
    roles: computed(() => props.roles),
    task: computed(() => props.task),
  };
}
