<script setup lang="ts">
import {
  ArrowLeftIcon,
  ArrowPathIcon,
  ArrowRightIcon,
  ClockIcon,
  UserGroupIcon,
  ArrowDownTrayIcon,
} from "@heroicons/vue/24/outline";
import { VueFlow } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { Controls } from "@vue-flow/controls";
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
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import "@vue-flow/controls/dist/style.css";

type Person = {
  userId: string;
  displayName?: string | null;
  email?: string | null;
  isActive: boolean;
};
type Role = { roleName: string };
type Policy = { id: string; content: string; active: boolean };
type AccessNode = {
  id: string;
  label: string;
  data: { kind: string; resourceId: string };
};
type SyncConfiguration = {
  connectionId: string;
  claim: string;
  enabled: boolean;
  mappings: { externalGroup: string; roleName: string }[];
};
const props = defineProps<{
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
}>();
const emit = defineEmits<{
  success: [message: string];
  failure: [message: string];
  settingsChanged: [];
  peopleChanged: [];
}>();
const mode = ref<"" | "sync" | "roles" | "rules">("");
const busy = ref(false);
const search = ref("");
const graph = ref<ReturnType<typeof accessGraphLayout>>({
  nodes: [],
  edges: [],
  truncated: false,
});
const selectedNode = ref<AccessNode>();
function selectAccessNode(id: string) {
  selectedNode.value = graph.value.nodes.find(
    (candidate: AccessNode) => candidate.id === id,
  );
}
const people = ref<Person[]>(props.users);
const roleOptions = ref<Role[]>(props.roles);
interface DirectoryPage<T> {
  items: T[];
  after: string | null;
}
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
const ruleName = ref("Office hours");
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
const choices = [
  {
    id: "sync",
    title: "Import permissions automatically",
    description:
      "Connect company groups to Galadril roles. New sign-ins receive the access you choose.",
    icon: ArrowDownTrayIcon,
  },
  {
    id: "roles",
    title: "Manage roles and people",
    description:
      "Create roles, give people access and explore how they connect to your data.",
    icon: UserGroupIcon,
  },
  {
    id: "rules",
    title: "Set general access rules",
    description:
      "Choose when people can use their access, with guided settings or Cedar code.",
    icon: ClockIcon,
  },
] as const;
const resources = computed(() =>
  graph.value.nodes.filter(
    (node: AccessNode) => resourceAccessOptions(node.data.kind).length,
  ),
);
const selectedResource = computed(() =>
  resources.value.find((node: AccessNode) => node.id === targetResource.value),
);
const resourcePermissions = computed(() =>
  resourceAccessOptions(selectedResource.value?.data.kind),
);
const resourceAccessReady = computed(() =>
  canSetResourceAccess(selectedResource.value?.data.kind, access.value),
);
watch(selectedResource, () => {
  access.value = resourcePermissions.value[0]?.value ?? "";
});
const resourceLabels: Record<string, string> = {
  user: "Person",
  role: "Role",
  group: "Data space",
  project: "Project",
  table: "Table",
  raw: "Source data",
  document: "Document",
  ontology: "Data model",
  pipeline: "Data pipeline",
  conversation: "Conversation",
  entity_state: "Data record",
  event: "Event",
};
const resourceLabel = (kind: string) => resourceLabels[kind] || "Resource";
const scopedRoles = computed(() =>
  ruleScope.value &&
  !ruleRoles.value.some((role) => role.roleName === ruleScope.value)
    ? [{ roleName: ruleScope.value }, ...ruleRoles.value]
    : ruleRoles.value,
);
const relatedEdges = computed(() =>
  graph.value.edges.filter(
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
    "Unavailable resource"
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
    if (success) emit("success", success);
    return true;
  } catch (error) {
    emit("failure", administrationError(error, fallback));
    return false;
  } finally {
    busy.value = false;
  }
}
async function loadGraph() {
  const epoch = ++directoryEpoch;
  const term = search.value;
  people.value = [];
  roleOptions.value = [];
  peopleAfter.value = null;
  rolesAfter.value = null;
  const data = await props.request<{
    accessGraph: { nodes: unknown[]; edges: unknown[]; truncated: boolean };
    users: DirectoryPage<Person>;
    roles: DirectoryPage<Role>;
  }>(
    "query($search: String) { administration { accessGraph(search: $search, limit: 100) { nodes { id label kind resourceId } edges { source target relation } truncated } users(search: $search, limit: 20) { items { userId displayName email isActive } after } roles(search: $search, limit: 20) { items { roleName } after } } }",
    { search: term || null },
  );
  if (epoch !== directoryEpoch || term !== search.value) return;
  graph.value = accessGraphLayout(data.accessGraph);
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
      : "query($search: String, $after: String) { administration { roles(search: $search, after: $after, limit: 20) { items { roleName } after } } }";
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
    "query($search: String, $after: String) { administration { roles(search: $search, after: $after, limit: 20) { items { roleName } after } } }",
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
      "The access map could not be loaded. Use Reload access map to try again.",
    );
  if (next === "rules")
    await perform(
      async () => {
        await loadPolicies();
        await loadRuleRoles();
      },
      "",
      "Your saved access rules could not be loaded. Go back and try again.",
    );
  if (next === "sync")
    await perform(
      async () => {
        const data = await props.request<{
          roleSyncConfigurations: SyncConfiguration[];
          roles: DirectoryPage<Role>;
          users: DirectoryPage<Person>;
        }>(
          "query { administration { roleSyncConfigurations { connectionId claim enabled mappings { externalGroup roleName } } users(limit: 20) { items { userId displayName email isActive } after } roles(limit: 20) { items { roleName } after } } }",
        );
        syncConfigurations.value = data.roleSyncConfigurations;
        ruleRoles.value = data.roles.items;
        ruleRolesAfter.value = data.roles.after;
        people.value = data.users.items;
        peopleAfter.value = data.users.after;
      },
      "",
      "Company role synchronization could not be loaded. Check your sign-in connection and try again.",
    );
}
watch(
  search,
  useDebounceFn(() => {
    if (mode.value === "roles")
      return perform(
        loadGraph,
        "",
        "Your search could not be completed. Use Reload access map to try again.",
      );
  }, 250),
);
watch(
  roleSearch,
  useDebounceFn(
    () =>
      perform(
        async () => {
          await loadRuleRoles();
        },
        "",
        "Roles could not be searched. Clear the search and try again.",
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
      emit("peopleChanged");
    },
    "Role created. Choose who should receive it below.",
    "This role could not be created. Use a unique name with letters, numbers, hyphens or underscores.",
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
      emit("peopleChanged");
    },
    enabled
      ? "Role assigned. The access map is up to date."
      : "Role removed. The access map is up to date.",
    "This role membership could not be changed. Check that the person and role still exist.",
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
    "Role inheritance updated.",
    "This role connection could not be saved. Choose different roles and avoid a circular chain of inherited access.",
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
      ? "Data access granted. The access map is up to date."
      : "Data access removed. The access map is up to date.",
    "This access change could not be saved. Select a visible person or role, resource and supported permission.",
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
    emit(
      "failure",
      error instanceof Error
        ? error.message
        : "Review the access hours before continuing.",
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
    "General access rule saved. Administrators keep access to manage the organization.",
    "This access rule could not be saved. Check the selected roles and hours, then review it again.",
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
    "Access policy saved. Administrators keep access to manage the organization.",
    "This Cedar policy could not be saved. Check its syntax and use only the supported principal, resource and context attributes.",
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
      emit("settingsChanged");
    },
    props.settings.permission_import
      ? "Permission import disabled. Existing access stays in place."
      : "Permission import enabled for your organization.",
    "Permission import settings could not be saved. Check your administrator access and try again.",
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
    "Company role synchronization saved. Mapped access is reconciled on the next company sign-in.",
    "Company roles could not be synchronized. Choose a connected provider and map each company group to an existing Galadril role.",
  );
}
function reviewPermissions() {
  try {
    permissionBlocks(manifest.value);
    reviewImport.value = true;
  } catch {
    emit(
      "failure",
      "Complete each permission change or correct the configuration before reviewing.",
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
      emit("peopleChanged");
      if (!result.complete) {
        emit(
          "failure",
          `${result.applied} changes applied; change ${(result.failedAt ?? 0) + 1} could not be applied. Earlier changes remain applied. Review the configuration before retrying.`,
        );
      } else emit("success", `${result.applied} access changes applied.`);
    },
    "",
    "The permission configuration could not be imported. Review each change and confirm that imports are enabled.",
  );
}
</script>

<template>
  <div class="space-y-6">
    <div v-if="mode === 'sync' || mode === 'rules'">
      <label for="configuration-role-search" class="sr-only"
        >Search configuration roles</label
      ><input
        id="configuration-role-search"
        v-model="roleSearch"
        type="search"
        placeholder="Find a role"
      />
      <UtilsButton
        v-if="mode === 'sync' && peopleAfter"
        variant="secondary"
        :disabled="busy"
        @click="
          perform(
            () => moreDirectory('users'),
            '',
            'More people could not be loaded.',
          )
        "
        >Load more people</UtilsButton
      >
      <UtilsButton
        v-if="ruleRolesAfter"
        variant="secondary"
        :disabled="busy"
        @click="
          perform(
            () => loadRuleRoles(true),
            '',
            'More roles could not be loaded.',
          )
        "
        >Load more roles</UtilsButton
      >
    </div>
    <section v-if="!mode" class="space-y-4" aria-label="Permission options">
      <div class="rounded-2xl border border-amber-100 bg-amber-50/60 p-5">
        <h2 class="font-semibold text-zinc-900">
          What would you like to configure?
        </h2>
        <p class="mt-2 text-sm leading-relaxed text-zinc-600">
          Start with one option. Roles decide what people can do. General rules
          decide when that access is available.
        </p>
      </div>
      <button
        v-for="choice in choices"
        :key="choice.id"
        type="button"
        class="choice"
        @click="choose(choice.id)"
      >
        <span class="choice-icon"
          ><component :is="choice.icon" class="h-6 w-6" aria-hidden="true"
        /></span>
        <span class="min-w-0 flex-1"
          ><strong class="block text-sm font-semibold text-zinc-900">{{
            choice.title
          }}</strong
          ><span class="mt-1 block text-sm leading-relaxed text-zinc-500">{{
            choice.description
          }}</span></span
        >
        <ArrowRightIcon
          class="h-5 w-5 shrink-0 text-zinc-400"
          aria-hidden="true"
        />
      </button>
    </section>
    <template v-else>
      <UtilsButton
        type="button"
        variant="ghost"
        :disabled="busy"
        @click="mode = ''"
        ><ArrowLeftIcon class="mr-2 h-4 w-4" aria-hidden="true" />All permission
        options</UtilsButton
      >
      <template v-if="mode === 'sync'">
        <section class="permission-card">
          <h2>Reuse your company’s groups</h2>
          <p>
            Your sign-in provider sends a person’s groups with their verified
            identity. Match those groups to the roles your team needs. Unmapped
            groups do not grant access.
          </p>
          <label class="check"
            ><input
              type="checkbox"
              :checked="settings.permission_import"
              :disabled="busy"
              @click.prevent="confirmImportSettings = true"
            />Allow permission imports</label
          >
          <p class="hint">
            Off by default. This enables approved imports and company role
            synchronization; it does not grant permissions by itself.
          </p>
          <ol class="steps" aria-label="Role synchronization progress">
            <li :aria-current="syncStep === 1 ? 'step' : undefined">
              1. Company sign-in
            </li>
            <li :aria-current="syncStep === 2 ? 'step' : undefined">
              2. Match groups
            </li>
            <li :aria-current="syncStep === 3 ? 'step' : undefined">
              3. Review
            </li>
          </ol>
          <fieldset :disabled="busy">
            <template v-if="syncStep === 1">
              <label for="sync-connection">Company sign-in connection</label
              ><select
                id="sync-connection"
                v-model="synchronization.connectionId"
                @change="selectConnection"
              >
                <option value="" disabled>
                  Choose a connected company domain
                </option>
                <option
                  v-for="connection in settings.connections.filter(
                    (item) => item.enabled && item.connection_id,
                  )"
                  :key="connection.connection_id"
                  :value="connection.connection_id"
                >
                  {{ connection.domain }}
                </option>
              </select>
              <p
                v-if="
                  !settings.connections.some(
                    (item) => item.enabled && item.connection_id,
                  )
                "
                class="hint"
              >
                Connect your company sign-in under Sign-in &amp; security before
                enabling automatic imports.
              </p>
              <details class="mt-4 rounded-xl border border-zinc-200 p-4">
                <summary class="cursor-pointer text-sm text-zinc-600">
                  Provider attribute · advanced
                </summary>
                <label for="sync-claim">Group attribute</label
                ><input
                  id="sync-claim"
                  v-model.trim="synchronization.claim"
                  maxlength="64"
                />
                <p class="hint">
                  Your IT team can confirm the signed group attribute. Polis
                  OIDC usually uses raw.groups. Keep the path supplied by your
                  provider if it differs.
                </p>
              </details>
              <UtilsButton
                class="mt-5"
                type="button"
                :disabled="!synchronization.connectionId"
                @click="syncStep = 2"
                >Continue</UtilsButton
              >
            </template>
            <template v-else-if="syncStep === 2">
              <h3>What should each company group be able to do?</h3>
              <p>
                Use the exact group name supplied by your company provider.
                Create any missing Galadril role from Manage roles and people
                first.
              </p>
              <div
                v-for="(mapping, index) in synchronization.mappings"
                :key="index"
                class="mt-4 grid gap-3 rounded-xl bg-stone-50 p-4 sm:grid-cols-2"
              >
                <label :for="`external-group-${index}`"
                  >Company group<input
                    :id="`external-group-${index}`"
                    v-model.trim="mapping.externalGroup"
                    maxlength="256"
                    required /></label
                ><label :for="`internal-role-${index}`"
                  >Galadril role<select
                    :id="`internal-role-${index}`"
                    v-model="mapping.roleName"
                    required
                  >
                    <option value="" disabled>Choose a role</option>
                    <option v-for="role in ruleRoles" :key="role.roleName">
                      {{ role.roleName }}
                    </option>
                  </select></label
                ><UtilsButton
                  type="button"
                  variant="ghost"
                  :aria-label="`Remove group match ${index + 1}`"
                  @click="synchronization.mappings.splice(index, 1)"
                  >Remove</UtilsButton
                >
              </div>
              <UtilsButton
                class="mt-4"
                type="button"
                variant="secondary"
                :disabled="synchronization.mappings.length >= 64"
                @click="
                  synchronization.mappings.push({
                    externalGroup: '',
                    roleName: '',
                  })
                "
                >Add a group match</UtilsButton
              >
              <div class="mt-5 flex gap-3">
                <UtilsButton
                  type="button"
                  variant="secondary"
                  @click="syncStep = 1"
                  >Back</UtilsButton
                ><UtilsButton
                  type="button"
                  :disabled="
                    !synchronization.mappings.length ||
                    synchronization.mappings.some(
                      (item) => !item.externalGroup || !item.roleName,
                    )
                  "
                  @click="syncStep = 3"
                  >Review matches</UtilsButton
                >
              </div>
            </template>
            <template v-else>
              <h3>Review company access</h3>
              <ul class="mt-4 space-y-2 text-sm text-zinc-600">
                <li
                  v-for="mapping in synchronization.mappings"
                  :key="mapping.externalGroup"
                >
                  <strong>{{ mapping.externalGroup }}</strong> →
                  {{ mapping.roleName }}
                </li>
              </ul>
              <label class="check"
                ><input
                  v-model="synchronization.enabled"
                  type="checkbox"
                />Synchronize these roles at company sign-in</label
              >
              <p class="hint">
                Only these mapped memberships are synchronized. Locally granted
                access stays in place. Administrator access must be managed
                inside Galadril.
              </p>
              <div class="mt-5 flex gap-3">
                <UtilsButton
                  type="button"
                  variant="secondary"
                  @click="syncStep = 2"
                  >Back</UtilsButton
                ><UtilsButton
                  type="button"
                  :disabled="
                    synchronization.enabled && !settings.permission_import
                  "
                  @click="saveSynchronization"
                  >Save synchronization</UtilsButton
                >
              </div>
            </template>
          </fieldset>
        </section>
        <details class="permission-card">
          <summary class="cursor-pointer text-sm font-medium text-zinc-700">
            Import a configuration · advanced
          </summary>
          <p>
            For your IT team or automation: prepare a compatible permission
            configuration, then review it before applying any changes.
          </p>
          <form class="mt-5 space-y-4" @submit.prevent="reviewPermissions">
            <AdministrationPermissionEditor
              v-model="manifest"
              :users="people"
              :roles="ruleRoles"
              :disabled="busy"
            /><UtilsButton
              type="submit"
              :disabled="
                busy || !settings.permission_import || !manifest.trim()
              "
              >Review changes</UtilsButton
            >
          </form>
        </details>
      </template>
      <template v-else-if="mode === 'roles'">
        <section class="permission-card">
          <div class="flex items-start justify-between gap-3">
            <div>
              <h2>See who can reach your data</h2>
              <p>
                Select a person, role or resource to inspect its connections.
                This map shows only access you are allowed to manage.
              </p>
            </div>
            <UtilsButton
              type="button"
              variant="secondary"
              aria-label="Reload access map"
              :aria-busy="busy"
              :disabled="busy"
              @click="
                perform(
                  loadGraph,
                  'Access map updated.',
                  'The access map could not be loaded. Try again.',
                )
              "
              ><ArrowPathIcon
                :class="[
                  'h-5 w-5',
                  { 'animate-spin motion-reduce:animate-none': busy },
                ]"
                aria-hidden="true"
            /></UtilsButton>
          </div>
          <UtilsButton
            v-if="peopleAfter"
            variant="secondary"
            :disabled="busy"
            @click="
              perform(
                () => moreDirectory('users'),
                '',
                'More people could not be loaded.',
              )
            "
            >Load more people</UtilsButton
          >
          <UtilsButton
            v-if="rolesAfter"
            variant="secondary"
            :disabled="busy"
            @click="
              perform(
                () => moreDirectory('roles'),
                '',
                'More roles could not be loaded.',
              )
            "
            >Load more roles</UtilsButton
          >
          <label class="sr-only" for="access-search"
            >Find people, roles or resources</label
          ><input
            id="access-search"
            v-model="search"
            type="search"
            class="mt-5"
            placeholder="Find a person, role or document"
            maxlength="128"
          />
          <div class="mt-4 flex flex-wrap gap-4 text-xs text-zinc-500">
            <span>● People</span><span class="text-amber-700">● Roles</span
            ><span>● Data spaces</span><span>● Resources</span>
          </div>
          <div
            class="mt-4 h-80 overflow-hidden rounded-xl border border-zinc-200 bg-stone-50"
            role="region"
            aria-label="Access map"
          >
            <VueFlow
              v-if="graph.nodes.length"
              :nodes="graph.nodes"
              :edges="graph.edges"
              :nodes-draggable="false"
              :nodes-connectable="false"
              fit-view-on-init
              :min-zoom="0.25"
              :max-zoom="1.5"
              @node-click="({ node }) => selectAccessNode(node.id)"
              ><Background pattern-color="#e7e5e4" :gap="20" /><Controls
                :show-interactive="false"
            /></VueFlow>
            <p v-else class="p-8 text-center">
              {{
                busy
                  ? "Loading access…"
                  : "No visible access connections match your search."
              }}
            </p>
          </div>
          <p v-if="graph.truncated" class="hint">
            This map shows a limited set of connections. Searching finds
            matching people and roles across your organization. Resource names
            filter the displayed connections.
          </p>
          <div v-if="selectedNode" class="mt-4 rounded-xl bg-amber-50/60 p-4">
            <h3>{{ selectedNode.label }}</h3>
            <p class="hint">
              {{ resourceLabel(selectedNode.data.kind) }}
            </p>
            <ul class="mt-2 space-y-2 text-sm text-zinc-700">
              <li v-for="edge in relatedEdges" :key="edge.id">
                {{ nodeLabel(edge.source) }} → {{ nodeLabel(edge.target) }}
                <span class="text-zinc-500">· {{ edge.label }}</span>
              </li>
            </ul>
            <p v-if="!relatedEdges.length" class="hint">
              No direct relationships are shown in this view.
            </p>
          </div>
        </section>
        <section class="permission-card">
          <h2>1. Choose a person or role</h2>
          <p>
            A role brings permissions together so you can manage a whole team at
            once.
          </p>
          <label for="access-subject">Who needs access?</label
          ><select id="access-subject" v-model="subject">
            <option value="" disabled>Choose a person or role</option>
            <optgroup label="People">
              <option
                v-for="person in people.filter((item) => item.isActive)"
                :key="person.userId"
                :value="`user:${person.userId}`"
              >
                {{ displayName(person) }}
              </option>
            </optgroup>
            <optgroup label="Roles">
              <option
                v-for="role in roleOptions"
                :key="role.roleName"
                :value="`role:${role.roleName}`"
              >
                {{ role.roleName }}
              </option>
            </optgroup>
          </select>
          <details class="mt-5 rounded-xl border border-zinc-200 p-4">
            <summary class="cursor-pointer text-sm font-medium text-zinc-700">
              Create a role
            </summary>
            <form class="mt-3" @submit.prevent="createRole">
              <label for="new-role">New role name</label
              ><input
                id="new-role"
                v-model.trim="newRole"
                pattern="[A-Za-z0-9_-]{1,64}"
                maxlength="64"
                required
                placeholder="For example, sales_analyst"
              />
              <p class="hint">
                Use letters, numbers, hyphens or underscores. A new role has no
                access until you grant it.
              </p>
              <UtilsButton
                class="mt-4"
                type="submit"
                :disabled="busy || !newRole"
                >Create role</UtilsButton
              >
            </form>
          </details>
        </section>
        <section v-if="subject" class="permission-card">
          <h2>2. Choose what they can access</h2>
          <template v-if="subject.startsWith('user:')"
            ><h3 class="mt-4">Give them a role</h3>
            <label for="grant-role">Role to assign</label
            ><select id="grant-role" v-model="selectedRole">
              <option value="" disabled>Choose a role</option>
              <option v-for="role in roleOptions" :key="role.roleName">
                {{ role.roleName }}
              </option>
            </select>
            <div class="mt-4 flex flex-wrap gap-3">
              <UtilsButton
                type="button"
                :disabled="busy || !selectedRole"
                @click="assignRole()"
                >Assign role</UtilsButton
              ><UtilsButton
                type="button"
                variant="secondary"
                :disabled="busy || !selectedRole"
                @click="assignRole(false)"
                >Remove role</UtilsButton
              >
            </div></template
          >
          <template v-else
            ><h3 class="mt-4">Include another role’s access</h3>
            <p class="hint">
              People with this role will also receive the permissions of the
              included role.
            </p>
            <label for="included-role">Role to include</label
            ><select id="included-role" v-model="parentRole">
              <option value="" disabled>Choose a role</option>
              <option
                v-for="role in roleOptions.filter(
                  (item) => `role:${item.roleName}` !== subject,
                )"
                :key="role.roleName"
              >
                {{ role.roleName }}
              </option>
            </select>
            <div class="mt-4 flex gap-3">
              <UtilsButton
                type="button"
                :disabled="busy || !parentRole"
                @click="
                  selectedRole = subject.slice(5);
                  includeRole();
                "
                >Include role</UtilsButton
              ><UtilsButton
                type="button"
                variant="secondary"
                :disabled="busy || !parentRole"
                @click="
                  selectedRole = subject.slice(5);
                  includeRole(false);
                "
                >Remove inclusion</UtilsButton
              >
            </div></template
          >
          <div class="mt-6 border-t border-zinc-100 pt-5">
            <h3>Grant access to specific data</h3>
            <label for="access-resource">Data resource</label
            ><select id="access-resource" v-model="targetResource">
              <option value="" disabled>Choose a visible resource</option>
              <option
                v-for="resource in resources"
                :key="resource.id"
                :value="resource.id"
              >
                {{ resource.label }} ·
                {{ resourceLabel(resource.data.kind) }}
              </option>
            </select>
            <p v-if="!resources.length" class="hint">
              No resources are visible in this search. Clear or change the
              search above to find existing resources.
            </p>
            <label for="resource-access">Permission</label
            ><select
              id="resource-access"
              v-model="access"
              :disabled="busy || !selectedResource"
              class="disabled:cursor-not-allowed disabled:bg-stone-50 disabled:text-zinc-500"
            >
              <option value="" disabled>
                {{
                  selectedResource
                    ? "Choose a permission"
                    : "Choose a data resource first"
                }}
              </option>
              <option
                v-for="permission in resourcePermissions"
                :key="permission.value"
                :value="permission.value"
              >
                {{ permission.label }}
              </option>
            </select>
            <div class="mt-4 flex flex-wrap gap-3">
              <UtilsButton
                type="button"
                class="disabled:cursor-not-allowed disabled:opacity-50"
                :disabled="busy || !resourceAccessReady"
                @click="saveResourceAccess()"
                >Grant data access</UtilsButton
              ><UtilsButton
                type="button"
                variant="secondary"
                class="disabled:cursor-not-allowed disabled:opacity-50"
                :disabled="busy || !resourceAccessReady"
                @click="saveResourceAccess(false)"
                >Remove data access</UtilsButton
              >
            </div>
          </div>
        </section>
      </template>
      <template v-else>
        <section class="permission-card">
          <h2>General access rules</h2>
          <p>
            These rules restrict existing access; they do not grant access to
            data. Administrators stay able to manage your organization.
          </p>
          <div
            class="mt-5 flex w-fit gap-1 rounded-lg bg-stone-100 p-1"
            aria-label="General rule editor"
          >
            <UtilsButton
              type="button"
              :variant="ruleMode === 'blocks' ? 'secondary' : 'ghost'"
              :aria-pressed="ruleMode === 'blocks'"
              @click="ruleMode = 'blocks'"
              >Guided settings</UtilsButton
            ><UtilsButton
              type="button"
              :variant="ruleMode === 'code' ? 'secondary' : 'ghost'"
              :aria-pressed="ruleMode === 'code'"
              @click="ruleMode = 'code'"
              >Cedar code</UtilsButton
            >
          </div>
          <template v-if="ruleMode === 'blocks'"
            ><ol class="steps" aria-label="General access rule progress">
              <li :aria-current="ruleStep === 1 ? 'step' : undefined">
                1. Who
              </li>
              <li :aria-current="ruleStep === 2 ? 'step' : undefined">
                2. When
              </li>
              <li :aria-current="ruleStep === 3 ? 'step' : undefined">
                3. Review
              </li>
            </ol>
            <fieldset :disabled="busy">
              <template v-if="ruleStep === 1"
                ><h3>Who should this rule apply to?</h3>
                <label for="rule-name">Rule name</label>
                <input
                  id="rule-name"
                  v-model="ruleName"
                  maxlength="128"
                  required
                  placeholder="For example, Office hours"
                />
                <p class="hint">
                  Give each access rule its own name. Reusing a saved name
                  updates that rule.
                </p>
                <label for="rule-role-search" class="sr-only">Find a role</label
                ><input
                  id="rule-role-search"
                  v-model="roleSearch"
                  type="search"
                  placeholder="Find a role"
                  maxlength="128"
                  class="mt-4"
                />
                <label for="rule-scope">Rule scope</label
                ><select id="rule-scope" v-model="ruleScope">
                  <option value="">Everyone except administrators</option>
                  <option v-for="role in scopedRoles" :key="role.roleName">
                    {{ role.roleName }}
                  </option>
                </select>
                <p class="hint">
                  Choose a team role or apply the rule to everyone. Use Manage
                  roles and people to create a role first.
                </p>
                <UtilsButton class="mt-5" type="button" @click="ruleStep = 2"
                  >Continue</UtilsButton
                ></template
              >
              <template v-else-if="ruleStep === 2"
                ><h3>When should access be available?</h3>
                <p>
                  Hours use UTC, so they stay consistent across locations and
                  daylight saving changes. A window crossing midnight is
                  supported.
                </p>
                <div class="grid gap-4 sm:grid-cols-2">
                  <label for="access-start"
                    >Access starts at (UTC)<input
                      id="access-start"
                      v-model="start"
                      type="time"
                      step="3600"
                      required /></label
                  ><label for="access-end"
                    >Access ends at (UTC)<input
                      id="access-end"
                      v-model="end"
                      type="time"
                      step="3600"
                      required
                  /></label>
                </div>
                <div
                  class="mt-5 rounded-xl border border-zinc-200 bg-stone-50 p-4"
                >
                  <h3>Approximate connection location</h3>
                  <p class="hint">
                    Location rules require a trusted location provider. This
                    deployment does not provide verified IP geolocation, so
                    location restrictions are unavailable.
                  </p>
                </div>
                <div class="mt-5 flex gap-3">
                  <UtilsButton
                    type="button"
                    variant="secondary"
                    @click="ruleStep = 1"
                    >Back</UtilsButton
                  ><UtilsButton type="button" @click="reviewRule"
                    >Review rule</UtilsButton
                  >
                </div></template
              >
              <template v-else
                ><h3>Review your access window</h3>
                <dl
                  class="mt-4 space-y-2 rounded-xl bg-amber-50/60 p-4 text-sm"
                >
                  <dt class="text-zinc-500">Rule name</dt>
                  <dd class="font-medium text-zinc-900">{{ ruleName }}</dd>
                  <dt class="text-zinc-500">Saved as</dt>
                  <dd class="font-medium text-zinc-900">{{ ruleId }}</dd>
                  <dt class="text-zinc-500">Applies to</dt>
                  <dd class="font-medium text-zinc-900">
                    {{ ruleScope || "Everyone except administrators" }}
                  </dd>
                  <dt class="text-zinc-500">Allowed hours</dt>
                  <dd class="font-medium text-zinc-900">
                    {{ start }}–{{ end }} UTC
                  </dd>
                </dl>
                <p class="hint">
                  {{
                    policies.some((policy) => policy.id === ruleId)
                      ? `Saving updates the existing rule ${ruleId}.`
                      : `Saving creates a new rule ${ruleId}.`
                  }}
                  Other saved rules continue to apply. Administrators keep
                  access.
                </p>
                <div class="mt-5 flex gap-3">
                  <UtilsButton
                    type="button"
                    variant="secondary"
                    @click="ruleStep = 2"
                    >Back</UtilsButton
                  ><UtilsButton type="button" @click="saveRule"
                    >Save rule</UtilsButton
                  >
                </div></template
              >
            </fieldset></template
          >
          <form v-else class="mt-5" @submit.prevent="savePolicy()">
            <p>
              For advanced conditions, use Cedar.
              <a
                href="https://docs.cedarpolicy.com/"
                target="_blank"
                rel="noopener noreferrer"
                class="font-medium text-amber-700 underline"
                >Read the Cedar documentation</a
              >.
            </p>
            <p class="hint">
              Available request attributes include UTC hour and verified region
              when provided. Never assume an unverified location is present.
            </p>
            <label for="cedar-id">Policy name</label
            ><input
              id="cedar-id"
              v-model.trim="policyId"
              maxlength="64"
              pattern="[A-Za-z0-9_-]{1,64}"
              required
            /><label for="cedar-code">Cedar policy</label
            ><textarea
              id="cedar-code"
              v-model="policyContent"
              rows="10"
              maxlength="8192"
              required
              spellcheck="false"
              placeholder="forbid(principal, action, resource) when { context.hour_utc < 8 };"
            /><label class="check"
              ><input v-model="policyActive" type="checkbox" />Enable this
              policy</label
            ><UtilsButton
              class="mt-5"
              type="submit"
              :disabled="busy || !policyContent.trim()"
              >Save Cedar policy</UtilsButton
            >
          </form>
        </section>
        <section v-if="policies.length" class="permission-card">
          <h2>Saved rules</h2>
          <p>
            Review the code or pause an existing rule. Your change takes effect
            without reloading the page.
          </p>
          <div
            v-for="policy in policies"
            :key="policy.id"
            class="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-100 py-4 last:border-0"
          >
            <span class="text-sm font-medium text-zinc-800"
              >{{ policy.id
              }}<span class="ml-2 text-xs font-normal text-zinc-500">{{
                policy.active ? "Active" : "Paused"
              }}</span></span
            >
            <div class="flex gap-2">
              <UtilsButton
                type="button"
                variant="ghost"
                :disabled="busy"
                @click="editPolicy(policy)"
                >Edit</UtilsButton
              ><UtilsButton
                type="button"
                variant="secondary"
                :disabled="busy"
                @click="savePolicy({ ...policy, active: !policy.active })"
                >{{ policy.active ? "Pause rule" : "Enable rule" }}</UtilsButton
              >
            </div>
          </div>
        </section>
      </template>
    </template>
    <UtilsModal
      :is-open="confirmImportSettings"
      :title="
        settings.permission_import
          ? 'Turn off permission imports?'
          : 'Allow permission imports?'
      "
      @close="confirmImportSettings = false"
    >
      <p class="text-sm text-zinc-600">
        {{
          settings.permission_import
            ? "Existing access stays in place. New imports and company role synchronization will be blocked."
            : "Administrators and authorized tools can apply reviewed permissions. Automatic imports only assign roles you explicitly match."
        }}
      </p>
      <template #footer
        ><UtilsButton
          type="button"
          variant="secondary"
          :disabled="busy"
          @click="confirmImportSettings = false"
          >Cancel</UtilsButton
        ><UtilsButton
          type="button"
          :disabled="busy"
          @click="toggleImports().then(() => (confirmImportSettings = false))"
          >{{ busy ? "Applying…" : "Confirm" }}</UtilsButton
        ></template
      >
    </UtilsModal>
    <UtilsModal
      :is-open="reviewImport"
      title="Apply these access changes?"
      @close="reviewImport = false"
      ><p class="text-sm text-zinc-600">
        Changes apply in order. If one fails, earlier changes remain applied.
        Review the result before retrying.
      </p>
      <template #footer
        ><UtilsButton
          type="button"
          variant="secondary"
          :disabled="busy"
          @click="reviewImport = false"
          >Cancel</UtilsButton
        ><UtilsButton
          type="button"
          :disabled="busy"
          @click="applyPermissions"
          >{{ busy ? "Applying…" : "Confirm" }}</UtilsButton
        ></template
      ></UtilsModal
    >
  </div>
</template>

<style scoped>
.permission-card {
  border: 1px solid #e4e4e7;
  border-radius: 1rem;
  padding: 1.5rem;
  background: white;
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.025);
}
.choice {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 1rem;
  border: 1px solid #e4e4e7;
  border-radius: 1rem;
  background: white;
  padding: 1.5rem;
  text-align: left;
  transition:
    border-color 150ms,
    background-color 150ms;
}
.choice:hover {
  border-color: #fcd34d;
  background: #fffbeb;
}
.choice:focus-visible {
  outline: 2px solid #d97706;
  outline-offset: 2px;
}
.choice-icon {
  display: flex;
  height: 3rem;
  width: 3rem;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  border-radius: 0.75rem;
  background: #fffbeb;
  color: #b45309;
}
h2 {
  font-size: 1rem;
  font-weight: 600;
  color: #18181b;
}
h3 {
  font-size: 0.875rem;
  font-weight: 600;
  color: #27272a;
}
p {
  margin-top: 0.5rem;
  color: #71717a;
  font-size: 0.875rem;
  line-height: 1.6;
}
p.hint {
  font-size: 0.75rem;
}
label:not(.sr-only) {
  display: block;
  margin: 1rem 0 0.4rem;
  font-size: 0.8125rem;
  font-weight: 500;
  color: #3f3f46;
}
input:not([type="checkbox"]),
select,
textarea {
  width: 100%;
  border: 1px solid #d4d4d8;
  border-radius: 0.5rem;
  padding: 0.65rem 0.75rem;
  background: white;
  color: #27272a;
  font-size: 0.875rem;
}
input:focus-visible,
select:focus-visible,
textarea:focus-visible {
  outline: 2px solid #d97706;
  outline-offset: 2px;
}
textarea {
  font-family: monospace;
  resize: vertical;
}
label.check {
  display: flex;
  gap: 0.6rem;
  align-items: center;
  font-weight: 400;
}
input[type="checkbox"] {
  accent-color: #d97706;
}
fieldset:disabled {
  opacity: 0.6;
}
.steps {
  display: flex;
  gap: 0.5rem;
  margin: 1.5rem 0;
  font-size: 0.75rem;
}
.steps li {
  flex: 1;
  border-radius: 0.5rem;
  padding: 0.6rem;
  background: #fafaf9;
  color: #71717a;
}
.steps li[aria-current] {
  background: #fffbeb;
  color: #92400e;
  box-shadow: inset 0 0 0 1px #fde68a;
}
:deep(.access-node) {
  border-color: #d4d4d8;
  border-radius: 0.75rem;
  background: white;
  color: #3f3f46;
  padding: 1rem;
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.04);
}
:deep(.access-node-role) {
  border-color: #fbbf24;
  background: #fffbeb;
}
:deep(.vue-flow__edge-text) {
  font-size: 10px;
  fill: #52525b;
}
:deep(.vue-flow__node.selected) {
  box-shadow: 0 0 0 2px #d97706;
}
</style>
