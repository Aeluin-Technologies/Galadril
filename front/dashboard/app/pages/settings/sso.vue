<script setup lang="ts">
import { ArrowPathIcon } from "@heroicons/vue/24/outline";
import { useDebounceFn } from "@vueuse/core";
import {
  administrationResult,
  administrationError,
  normalizeCompanyDomain,
  normalizeProviderAddress,
  normalizeDiscoveryAddress,
  providerFormValues,
} from "~/utils/administration.mjs";
import { accountInitials } from "~/utils/account.mjs";
definePageMeta({ middleware: ["administrator"] });
useHead({ title: "Administration · Galadril" });
const activeSection = ref("organization");
const panel = ref<HTMLElement>();
const sections = [
  {
    id: "organization",
    label: "Organization",
    description: "Your team's home",
  },
  { id: "users", label: "People", description: "Members and their access" },
  {
    id: "domains",
    label: "Sign-in & security",
    description: "How your team connects",
  },
  {
    id: "permissions",
    label: "Permissions",
    description: "Who can access what",
  },
];
const route = useRoute();
watch(
  () => route.hash,
  (hash) => {
    const section = hash.slice(1);
    activeSection.value = sections.some((item) => item.id === section)
      ? section
      : "organization";
    nextTick(() => panel.value?.scrollTo({ top: 0 }));
  },
  { immediate: true },
);
interface Connection {
  connection_id?: string;
  domain: string;
  protocol: string;
  enabled: boolean;
  jit_provision: boolean;
}
interface Settings {
  organization_name: string;
  passkey_mfa: boolean;
  permission_import: boolean;
  connections: Connection[];
}
interface Challenge {
  record: string;
  value: string;
}
const settings = ref<Settings>();
const challenge = ref<Challenge>();
const busy = ref(false);
const message = ref("");
const failed = ref(false);
const domain = ref("");
const protocol = ref("oidc");
const issuer = ref("");
const domainError = ref("");
const issuerError = ref("");
const detectionAddress = ref("");
const prefilled = ref(false);
interface ProviderPreview {
  domain: string;
  domain_verified: boolean;
  configuration: {
    protocol: "oidc" | "saml";
    issuer?: string;
    entity_id?: string;
    sign_in_url: string;
    jwks_uri?: string;
    automatic_registration?: boolean;
    signing_certificates?: number;
    saml_metadata?: string;
  };
}
const detectedProvider = ref<ProviderPreview>();
async function detectProvider() {
  if (!normalizeDomainInput()) return;
  await run(
    async () => {
      const address = normalizeDiscoveryAddress(detectionAddress.value);
      detectionAddress.value = address;
      const requestedDomain = domain.value;
      detectedProvider.value = undefined;
      const data = await graphql<{ detectSsoProvider: ProviderPreview }>(
        "mutation($domain: String!, $address: String!) { administration { detectSsoProvider(domain: $domain, address: $address) { domain domain_verified: domainVerified configuration { ... on OidcProvider { protocol issuer sign_in_url: signInUrl jwks_uri: jwksUri automatic_registration: automaticRegistration } ... on SamlProvider { protocol entity_id: entityId sign_in_url: signInUrl signing_certificates: signingCertificates saml_metadata: samlMetadata } } } } }",
        { domain: requestedDomain, address },
      );
      if (
        domain.value !== requestedDomain ||
        detectionAddress.value !== address
      )
        throw new Error("Provider address changed");
      const preview = data.detectSsoProvider;
      if (preview.domain !== requestedDomain) throw new Error("Domain changed");
      providerFormValues(preview.configuration);
      detectedProvider.value = preview;
    },
    "",
    "We couldn’t verify a provider at this address. Paste its OpenID discovery or SAML metadata URL, or enter the settings manually.",
  );
}
function useDetectedProvider() {
  const preview = detectedProvider.value;
  if (!preview || preview.domain !== domain.value) return;
  const values = providerFormValues(preview.configuration);
  if (protocol.value !== values.protocol || issuer.value !== values.issuer) {
    clientId.value = "";
    secret.value = "";
    registrationToken.value = "";
  }
  protocol.value = values.protocol;
  issuer.value = values.issuer;
  metadata.value = values.metadata;
  prefilled.value = true;
  detectedProvider.value = undefined;
  message.value =
    "Detected settings filled in. Review them below before saving.";
}
function normalizeDomainInput() {
  try {
    domain.value = normalizeCompanyDomain(domain.value);
    domainError.value = "";
    return true;
  } catch {
    domainError.value =
      "Enter a company domain or work email, such as alex@company.com.";
    return false;
  }
}
function normalizeIssuerInput() {
  try {
    issuer.value = normalizeProviderAddress(issuer.value);
    issuerError.value = "";
    return true;
  } catch {
    issuerError.value =
      "Enter your provider's HTTPS address without a username, password or query parameters.";
    return false;
  }
}
const providerAddressValid = computed(() => {
  try {
    normalizeProviderAddress(issuer.value);
    return true;
  } catch {
    return false;
  }
});
function reviewConnection() {
  if (protocol.value === "oidc" && !normalizeIssuerInput()) return;
  providerStep.value = 3;
}
const clientId = ref("");
const secret = ref("");
const registrationToken = ref("");
const metadata = ref("");
const jit = ref(false);
const linkDomain = ref("");
const externalSubject = ref("");
const userId = ref("");
const organizationName = ref("");
const providerStep = ref(1);
const userSearch = ref("");
interface Member {
  userId: string;
  displayName?: string | null;
  email?: string | null;
  isActive: boolean;
}
const users = ref<Member[]>([]);
const peopleLoaded = ref(false);
const peopleRefreshing = ref(false);
const roles = ref<{ roleName: string }[]>([]);
const assignedRoles = ref<string[]>([]);
const peopleAfter = ref<string | null>(null);
const rolesAfter = ref<string | null>(null);
const roleSearch = ref("");
const rolesRefreshing = ref(false);
let peopleEpoch = 0;
let rolesEpoch = 0;
const selectedUser = ref<Member>();
const { session: account } = useAccount();
const visibleUsers = computed(() => users.value);
interface DirectoryPage<T> {
  items: T[];
  after: string | null;
}
const pending = ref<{
  title: string;
  description: string;
  action: () => Promise<void>;
  success: string;
}>();
function confirm(
  title: string,
  description: string,
  action: () => Promise<void>,
  success: string,
) {
  failed.value = false;
  message.value = "";
  pending.value = { title, description, action, success };
}
async function applyConfirmed() {
  const change = pending.value;
  if (!change) return;
  if (await run(change.action, change.success)) pending.value = undefined;
}
async function refreshPeople(append = false) {
  if (append && (peopleRefreshing.value || !peopleAfter.value)) return;
  const epoch = ++peopleEpoch;
  const search = userSearch.value;
  const after = append ? peopleAfter.value : null;
  peopleRefreshing.value = true;
  if (!append) {
    users.value = [];
    peopleAfter.value = null;
  }
  try {
    const data = await graphql<{ users: DirectoryPage<Member> }>(
      "query($search: String, $after: String) { administration { users(search: $search, after: $after, limit: 20) { items { userId displayName email isActive } after } } }",
      { search: search || null, after },
    );
    if (epoch !== peopleEpoch || search !== userSearch.value) return;
    peopleLoaded.value = true;
    users.value = append
      ? [...users.value, ...data.users.items]
      : data.users.items;
    peopleAfter.value = data.users.after;
    if (selectedUser.value) {
      const updated = users.value.find(
        (user) => user.userId === selectedUser.value?.userId,
      );
      if (updated) selectedUser.value = updated;
    }
  } catch (error) {
    if (epoch === peopleEpoch) {
      users.value = [];
      peopleAfter.value = null;
    }
    throw error;
  } finally {
    if (epoch === peopleEpoch) peopleRefreshing.value = false;
  }
}
async function refreshUserRoles(append = false) {
  const subject = selectedUser.value?.userId;
  if (!subject || (append && (rolesRefreshing.value || !rolesAfter.value)))
    return;
  const epoch = ++rolesEpoch;
  const search = roleSearch.value;
  const after = append ? rolesAfter.value : null;
  rolesRefreshing.value = true;
  if (!append) {
    roles.value = [];
    assignedRoles.value = [];
    rolesAfter.value = null;
  }
  try {
    const data = await graphql<{
      roles: DirectoryPage<{ roleName: string }> & { assignedRoles: string[] };
    }>(
      "query($search: String, $after: String, $subject: String!) { administration { roles(search: $search, after: $after, limit: 20) { items { roleName } after assignedRoles(userId: $subject) } } }",
      { search: search || null, after, subject },
    );
    if (
      epoch !== rolesEpoch ||
      subject !== selectedUser.value?.userId ||
      search !== roleSearch.value
    )
      return;
    roles.value = append
      ? [...roles.value, ...data.roles.items]
      : data.roles.items;
    assignedRoles.value = append
      ? [...assignedRoles.value, ...data.roles.assignedRoles]
      : data.roles.assignedRoles;
    rolesAfter.value = data.roles.after;
  } catch (error) {
    if (epoch === rolesEpoch) {
      roles.value = [];
      assignedRoles.value = [];
      rolesAfter.value = null;
    }
    throw error;
  } finally {
    if (epoch === rolesEpoch) rolesRefreshing.value = false;
  }
}
function openUser(user: Member) {
  selectedUser.value = user;
  roleSearch.value = "";
  void run(
    () => refreshUserRoles(),
    "",
    "This person’s access could not be loaded. Close and reopen this panel to retry.",
  );
}
watch(
  userSearch,
  useDebounceFn(
    () =>
      refreshPeople().catch((error) => {
        failed.value = true;
        message.value = administrationError(
          error,
          "Your team could not be loaded. Use Reload people to try again.",
        );
      }),
    250,
  ),
);
watch(
  roleSearch,
  useDebounceFn(
    () =>
      refreshUserRoles().catch((error) => {
        failed.value = true;
        message.value = administrationError(
          error,
          "Roles could not be loaded. Retry your search.",
        );
      }),
    250,
  ),
);
function label(user: Member) {
  return user.displayName || user.email || user.userId;
}
function hasRole(user: string, role: string) {
  return (
    selectedUser.value?.userId === user && assignedRoles.value.includes(role)
  );
}
function changeRole(user: Member, role: string, enabled: boolean) {
  confirm(
    enabled ? "Grant this role?" : "Remove this role?",
    `${label(user)} will ${enabled ? "receive" : "lose"} the permissions associated with ${role}.`,
    async () => {
      await graphql(
        enabled
          ? "mutation($user: String!, $role: String!) { administration { assignRoleToUser(userId: $user, roleName: $role) } }"
          : "mutation($user: String!, $role: String!) { administration { unassignRoleFromUser(userId: $user, roleName: $role) } }",
        { user: user.userId, role },
      );
      await refreshUserRoles();
    },
    "Access updated.",
  );
}
function changeStatus(user: Member) {
  confirm(
    user.isActive ? "Suspend this person?" : "Restore access?",
    user.isActive
      ? `${label(user)} will no longer be able to use Galadril. Their data and account will be kept.`
      : `${label(user)} will be able to sign in again with their existing account.`,
    async () => {
      await graphql(
        "mutation($user: String!, $active: Boolean!) { administration { updateUser(userId: $user, isActive: $active) } }",
        { user: user.userId, active: !user.isActive },
      );
      await refreshPeople();
    },
    user.isActive ? "Access suspended." : "Access restored.",
  );
}
function resetKey(user: Member) {
  confirm(
    "Reset this person’s passkey?",
    `${label(user)} must create a new passkey after their next sign-in. Verify their identity through your organization’s recovery process first.`,
    async () => {
      await graphql(
        "mutation($user: String!) { administration { resetUserPasskeys(userId: $user) } }",
        { user: user.userId },
      );
    },
    "Passkey reset. A new key will be requested at the next sign-in.",
  );
}
async function saveOrganization() {
  await run(
    async () => {
      await graphql(
        "mutation($name: String!) { administration { updateOrganization(name: $name) } }",
        { name: organizationName.value },
      );
      await refresh();
    },
    "Organization saved.",
    "Your organization name could not be saved. Use between 1 and 128 characters, then try again.",
  );
}
function changeMfa(enabled: boolean) {
  confirm(
    enabled
      ? "Require a passkey for your team?"
      : "Turn off the passkey requirement?",
    enabled
      ? "Everyone will need a passkey after signing in. People without one will create it during their next sign-in."
      : "Your team will continue to sign in using your company provider or password.",
    async () => {
      await graphql(
        "mutation($enabled: Boolean!) { administration { setPasskeyMfa(enabled: $enabled) } }",
        { enabled },
      );
      if (enabled) await useSessionState().requirePasskey();
      await refresh();
    },
    "Sign-in security updated.",
  );
}

async function graphql<T>(
  query: string,
  variables: Record<string, unknown> = {},
): Promise<T> {
  const response = await $fetch<{
    data?: { administration?: T };
    errors?: unknown[];
  }>("/graphql", {
    method: "POST",
    credentials: "same-origin",
    body: { query, variables },
  });
  return administrationResult(response) as T;
}
async function refresh() {
  const data = await graphql<{ ssoSettings: Settings }>(
    "query { administration { ssoSettings { organization_name: organizationName permission_import: permissionImport passkey_mfa: passkeyMfa connections { connection_id: connectionId domain protocol enabled jit_provision: jitProvision } } } }",
  );
  settings.value = data.ssoSettings;
  organizationName.value = settings.value.organization_name;
}
async function run(
  action: () => Promise<void>,
  success: string,
  failureMessage = "We couldn’t complete this request. Please try again or check your administrator access.",
) {
  if (busy.value) return false;
  busy.value = true;
  failed.value = false;
  message.value = "";
  try {
    await action();
    if (success) message.value = success;
    return true;
  } catch (error) {
    failed.value = true;
    message.value = administrationError(error, failureMessage);
    return false;
  } finally {
    busy.value = false;
  }
}
async function requestDomain() {
  if (!normalizeDomainInput()) return;
  await run(async () => {
    const data = await graphql<{ requestSsoDomain: Challenge }>(
      "mutation($domain: String!) { administration { requestSsoDomain(domain: $domain) { record value } } }",
      { domain: domain.value },
    );
    challenge.value = data.requestSsoDomain;
  }, "Publish this DNS TXT record, then save your provider. The verification expires after 24 hours.");
}
async function saveConnection() {
  if (
    !normalizeDomainInput() ||
    (protocol.value === "oidc" && !normalizeIssuerInput())
  )
    return;
  await run(async () => {
    const configuration =
      protocol.value === "oidc"
        ? {
            domain: domain.value,
            oidcIssuer: issuer.value,
            ...(clientId.value || secret.value
              ? {
                  oidcClientId: clientId.value,
                  oidcClientSecret: secret.value,
                }
              : {}),
            ...(registrationToken.value
              ? { oidcRegistrationToken: registrationToken.value }
              : {}),
            jitProvision: jit.value,
          }
        : {
            domain: domain.value,
            samlMetadata: metadata.value,
            jitProvision: jit.value,
          };
    try {
      await graphql(
        "mutation($configuration: SsoConnectionInput!) { administration { configureSsoConnection(configuration: $configuration) } }",
        { configuration },
      );
      await refresh();
    } finally {
      clientId.value = "";
      secret.value = "";
      registrationToken.value = "";
    }
  }, "Connection saved. Your verified email domain now selects this provider.");
}
function disable(domain: string) {
  confirm(
    "Disconnect company sign-in?",
    `New sign-ins for ${domain} will stop. Existing member accounts and their data will be kept.`,
    async () => {
      await graphql(
        "mutation($domain: String!) { administration { disableSsoConnection(domain: $domain) } }",
        { domain },
      );
      await refresh();
    },
    "New sign-ins are disabled for this domain.",
  );
}
async function linkIdentity() {
  await run(async () => {
    await graphql(
      "mutation($domain: String!, $externalSubject: String!, $userId: String!) { administration { linkExternalIdentity(domain: $domain, externalSubject: $externalSubject, userId: $userId) } }",
      {
        domain: linkDomain.value,
        externalSubject: externalSubject.value,
        userId: userId.value,
      },
    );
    externalSubject.value = "";
    userId.value = "";
  }, "External identity linked to the existing account.");
}
onMounted(() =>
  run(async () => {
    await refresh();
    await refreshPeople();
  }, ""),
);
</script>
<template>
  <section
    ref="panel"
    :id="activeSection"
    aria-label="Administration"
    class="h-full min-h-0 overflow-y-auto bg-stone-50"
  >
    <div class="mx-auto max-w-6xl px-5 py-8 sm:px-8 lg:py-10">
      <div class="flex flex-col gap-8 lg:flex-row lg:gap-12">
        <nav
          aria-label="Organization settings"
          class="flex gap-2 overflow-x-auto pb-2 lg:w-52 lg:shrink-0 lg:flex-col lg:self-start lg:sticky lg:top-8"
        >
          <NuxtLink
            v-for="section in sections"
            :key="section.id"
            :to="`/settings/sso#${section.id}`"
            :aria-current="activeSection === section.id ? 'page' : undefined"
            :class="[
              'shrink-0 rounded-xl px-4 py-3 text-sm transition',
              activeSection === section.id
                ? 'bg-amber-50 text-amber-800 ring-1 ring-inset ring-amber-200'
                : 'text-zinc-600 hover:bg-stone-100',
            ]"
          >
            <span class="font-medium">{{ section.label }}</span
            ><span class="mt-1 hidden text-xs text-zinc-500 lg:block">{{
              section.description
            }}</span>
          </NuxtLink>
        </nav>
        <div class="min-w-0 flex-1 space-y-6">
          <header>
            <h1 class="text-2xl font-semibold tracking-tight text-zinc-950">
              {{
                activeSection === "organization"
                  ? "Your organization"
                  : activeSection === "users"
                    ? "People"
                    : activeSection === "domains"
                      ? "Sign-in & security"
                      : "Permissions"
              }}
            </h1>
            <p class="mt-2 text-sm leading-relaxed text-zinc-500">
              {{
                activeSection === "organization"
                  ? "A few simple settings to make Galadril work for your team."
                  : activeSection === "users"
                    ? "See who can use Galadril and manage what they can access."
                    : activeSection === "domains"
                      ? "Let your team sign in with the account they already use at work."
                      : "Choose who can access your data and when. We’ll guide you through each change."
              }}
            </p>
          </header>
          <p
            v-if="message"
            :role="failed ? 'alert' : 'status'"
            :class="[
              'sticky top-3 z-10 rounded-xl border p-4 text-sm shadow-sm',
              failed
                ? 'border-red-200 bg-red-50 text-red-800'
                : 'border-amber-200 bg-amber-50 text-amber-900',
            ]"
          >
            {{ message }}
          </p>
          <p
            v-if="!settings && busy"
            role="status"
            class="text-sm text-zinc-500"
          >
            Loading your organization…
          </p>
          <UtilsButton
            v-if="failed && !settings"
            type="button"
            variant="secondary"
            :disabled="busy"
            @click="
              run(async () => {
                await refresh();
                await refreshPeople();
              }, '')
            "
            >Try again</UtilsButton
          >
          <template v-if="settings">
            <template v-if="activeSection === 'organization'">
              <form class="admin-card" @submit.prevent="saveOrganization">
                <h2>Make it yours</h2>
                <p>This name helps your team recognize their organization.</p>
                <label for="organization-name">Organization name</label>
                <input
                  id="organization-name"
                  v-model.trim="organizationName"
                  required
                  maxlength="128"
                  placeholder="Your company or team name"
                  autocomplete="organization"
                />
                <UtilsButton
                  type="submit"
                  :disabled="busy || !organizationName.trim()"
                  class="mt-5"
                  >Save changes</UtilsButton
                >
              </form>
              <section class="admin-card">
                <h2>Set up your team</h2>
                <p>
                  Start here. You can come back and change these settings at any
                  time.
                </p>
                <NuxtLink
                  v-for="(item, index) in [
                    {
                      id: 'users',
                      title: 'Meet your members',
                      text: 'Review accounts and give each person the access they need.',
                    },
                    {
                      id: 'domains',
                      title: 'Connect your company sign-in',
                      text: 'Follow three steps to use your existing work accounts.',
                    },
                    {
                      id: 'permissions',
                      title: 'Choose your team’s permissions',
                      text: 'Reuse company roles, manage access or set general rules.',
                    },
                  ]"
                  :key="item.id"
                  :to="`/settings/sso#${item.id}`"
                  class="mt-4 flex items-start gap-4 rounded-xl border border-zinc-100 p-4 hover:border-amber-200 hover:bg-amber-50/50 transition"
                >
                  <span
                    class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-stone-100 text-xs font-semibold text-zinc-500"
                    >{{ index + 1 }}</span
                  >
                  <span
                    ><strong class="block text-sm font-medium text-zinc-900">{{
                      item.title
                    }}</strong
                    ><span
                      class="mt-1 block text-xs leading-relaxed text-zinc-500"
                      >{{ item.text }}</span
                    ></span
                  >
                </NuxtLink>
              </section>
            </template>
            <template v-else-if="activeSection === 'users'">
              <section class="admin-card">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2>Your team</h2>
                    <p>
                      {{
                        peopleLoaded
                          ? `${users.length} account${users.length === 1 ? "" : "s"}`
                          : "Loading accounts…"
                      }}
                      · suspended accounts keep their data.
                    </p>
                  </div>
                  <UtilsButton
                    type="button"
                    variant="secondary"
                    :disabled="busy || peopleRefreshing"
                    aria-label="Reload people"
                    :aria-busy="peopleRefreshing"
                    @click="
                      run(
                        refreshPeople,
                        'People updated.',
                        'Your team could not be loaded. Use Reload people to try again.',
                      )
                    "
                    ><ArrowPathIcon
                      aria-hidden="true"
                      :class="[
                        'h-5 w-5',
                        {
                          'animate-spin motion-reduce:animate-none':
                            peopleRefreshing,
                        },
                      ]"
                  /></UtilsButton>
                </div>
                <label for="member-search" class="sr-only">Search people</label
                ><input
                  id="member-search"
                  v-model="userSearch"
                  type="search"
                  placeholder="Find a person by name or email"
                  class="mt-5"
                />
                <div class="mt-5 overflow-x-auto">
                  <table class="w-full text-left text-sm">
                    <thead
                      class="border-b border-zinc-200 text-xs text-zinc-500"
                    >
                      <tr>
                        <th class="py-3 font-medium">Person</th>
                        <th class="py-3 font-medium">Status</th>
                        <th class="py-3 font-medium text-right">Access</th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-zinc-100">
                      <tr v-for="user in visibleUsers" :key="user.userId">
                        <td class="py-4 pr-3">
                          <div class="flex items-center gap-3">
                            <span
                              class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-stone-100 text-xs font-semibold text-zinc-600"
                              >{{ accountInitials(label(user)) }}</span
                            >
                            <div class="min-w-0">
                              <span
                                class="block max-w-64 truncate font-medium text-zinc-800"
                                >{{ label(user)
                                }}<span
                                  v-if="user.userId === account?.subject"
                                  class="ml-2 text-xs font-normal text-zinc-400"
                                  >You</span
                                ></span
                              ><span
                                v-if="user.displayName && user.email"
                                class="block text-xs text-zinc-500"
                                >{{ user.email }}</span
                              >
                            </div>
                          </div>
                        </td>
                        <td class="py-4">
                          <span
                            :class="[
                              'rounded-full px-2 py-1 text-xs',
                              user.isActive
                                ? 'bg-emerald-50 text-emerald-700'
                                : 'bg-stone-100 text-zinc-500',
                            ]"
                            >{{ user.isActive ? "Active" : "Suspended" }}</span
                          >
                        </td>
                        <td class="py-4 text-right">
                          <UtilsButton
                            type="button"
                            variant="ghost"
                            :aria-label="`Manage ${label(user)}`"
                            @click="openUser(user)"
                            >Manage</UtilsButton
                          >
                        </td>
                      </tr>
                    </tbody>
                  </table>
                  <p
                    v-if="!visibleUsers.length"
                    class="py-8 text-center text-sm text-zinc-500"
                  >
                    {{
                      peopleLoaded
                        ? "No people match your search."
                        : busy
                          ? "Loading your team…"
                          : "The team could not be loaded. Use Reload people to try again."
                    }}
                  </p>
                </div>
                <UtilsButton
                  v-if="peopleAfter"
                  type="button"
                  variant="secondary"
                  :disabled="peopleRefreshing"
                  @click="
                    run(
                      () => refreshPeople(true),
                      '',
                      'More people could not be loaded. Try again.',
                    )
                  "
                  >Load more people</UtilsButton
                >
                <p class="mt-4 text-xs">
                  Accounts come from your connected sign-in provider.
                </p>
              </section>
              <details class="admin-card">
                <summary
                  class="cursor-pointer text-sm font-medium text-zinc-700"
                >
                  Link an existing account · advanced
                </summary>
                <p class="mt-3">
                  For your IT administrator: connect a provider account to an
                  existing member without creating a duplicate.
                </p>
                <form @submit.prevent="linkIdentity">
                  <fieldset :disabled="busy">
                    <label for="link-domain">Company domain</label
                    ><select id="link-domain" v-model="linkDomain" required>
                      <option value="" disabled>
                        Choose a connected domain
                      </option>
                      <option
                        v-for="connection in settings.connections"
                        :key="connection.domain"
                      >
                        {{ connection.domain }}
                      </option></select
                    ><label for="link-subject"
                      >Identity provider account identifier</label
                    ><input
                      id="link-subject"
                      v-model="externalSubject"
                      required
                      maxlength="1024"
                      autocomplete="off"
                    /><label for="link-user">Existing member</label
                    ><select id="link-user" v-model="userId" required>
                      <option value="" disabled>Choose a person</option>
                      <option
                        v-for="user in users"
                        :key="user.userId"
                        :value="user.userId"
                      >
                        {{ label(user) }}
                      </option></select
                    ><UtilsButton type="submit" class="mt-5"
                      >Link account</UtilsButton
                    >
                  </fieldset>
                </form>
              </details>
            </template>
            <template v-else-if="activeSection === 'domains'">
              <section class="admin-card">
                <h2>An extra check after sign-in</h2>
                <p>
                  A passkey uses a fingerprint, face, device PIN or security key
                  to confirm it is really you.
                </p>
                <label class="check mt-5"
                  ><input
                    type="checkbox"
                    :checked="settings.passkey_mfa"
                    :disabled="busy"
                    @click.prevent="changeMfa(!settings.passkey_mfa)"
                  />Require a passkey for everyone</label
                >
                <p class="hint">
                  Optional and off by default. People without a key create one
                  after signing in. If a key is lost, an administrator can reset
                  it from People.
                </p>
              </section>
              <section v-if="settings.connections.length" class="admin-card">
                <h2>Connected domains</h2>
                <div
                  v-for="connection in settings.connections"
                  :key="connection.domain"
                  class="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-100 py-4 last:border-0"
                >
                  <div>
                    <strong class="text-sm font-medium text-zinc-800">{{
                      connection.domain
                    }}</strong>
                    <p class="hint">
                      {{
                        connection.enabled
                          ? "Your team can sign in"
                          : "New sign-ins are disabled"
                      }}
                    </p>
                  </div>
                  <UtilsButton
                    v-if="connection.enabled"
                    type="button"
                    variant="secondary"
                    :disabled="busy"
                    @click="disable(connection.domain)"
                    >Disconnect</UtilsButton
                  >
                </div>
              </section>
              <section class="admin-card">
                <h2>Connect company sign-in</h2>
                <p>
                  Your IT team may need to help with the domain and sign-in
                  details.
                </p>
                <ol
                  aria-label="Sign-in setup progress"
                  class="my-6 flex gap-2 text-xs"
                >
                  <li
                    v-for="(step, index) in [
                      'Your domain',
                      'Your provider',
                      'Review',
                    ]"
                    :key="step"
                    :aria-current="
                      providerStep === index + 1 ? 'step' : undefined
                    "
                    :class="[
                      'flex-1 rounded-lg px-3 py-2',
                      providerStep === index + 1
                        ? 'bg-amber-50 text-amber-800 ring-1 ring-amber-200'
                        : 'bg-stone-50 text-zinc-400',
                    ]"
                  >
                    {{ index + 1 }}. {{ step }}
                  </li>
                </ol>
                <form @submit.prevent="saveConnection">
                  <fieldset :disabled="busy">
                    <div v-if="providerStep === 1">
                      <h3>What email domain does your team use?</h3>
                      <p>
                        Enter your domain, work email or company website. We
                        keep the domain and check that your organization owns
                        it.
                      </p>
                      <label for="sso-domain">Company email domain</label
                      ><input
                        id="sso-domain"
                        v-model="domain"
                        required
                        maxlength="2048"
                        placeholder="company.com or alex@company.com"
                        autocapitalize="none"
                        spellcheck="false"
                        :aria-invalid="Boolean(domainError)"
                        aria-describedby="sso-domain-hint"
                        @blur="domain && normalizeDomainInput()"
                        @input="challenge = undefined"
                      />
                      <p
                        id="sso-domain-hint"
                        class="hint"
                        :role="domainError ? 'alert' : undefined"
                      >
                        {{
                          domainError ||
                          "For alex@company.com, we use company.com."
                        }}
                      </p>
                      <UtilsButton
                        type="button"
                        variant="secondary"
                        class="mt-4"
                        :disabled="!domain"
                        @click="requestDomain"
                        >Get verification instructions</UtilsButton
                      >
                      <div
                        v-if="challenge"
                        class="mt-5 rounded-xl bg-stone-50 p-4"
                      >
                        <h3>
                          Ask the person managing your domain to add this record
                        </h3>
                        <p class="hint">
                          In your domain's DNS settings, add a TXT record with
                          these values. We check it when you finish. These
                          instructions expire after 24 hours.
                        </p>
                        <dl class="mt-3 space-y-2 text-xs">
                          <dt class="text-zinc-500">Record name</dt>
                          <dd
                            class="break-all rounded border border-zinc-200 bg-white p-2 font-mono"
                          >
                            {{ challenge.record }}
                          </dd>
                          <dt class="text-zinc-500">Value</dt>
                          <dd
                            class="break-all rounded border border-zinc-200 bg-white p-2 font-mono"
                          >
                            {{ challenge.value }}
                          </dd>
                        </dl>
                      </div>
                      <UtilsButton
                        type="button"
                        :disabled="!challenge"
                        class="mt-5"
                        @click="providerStep = 2"
                        >Continue</UtilsButton
                      >
                    </div>
                    <div v-else-if="providerStep === 2">
                      <h3>Where does your team sign in?</h3>
                      <p>
                        Your IT team can provide the address and any credentials
                        required by your sign-in service.
                      </p>
                      <label for="sso-detection"
                        >Provider or metadata address</label
                      >
                      <input
                        id="sso-detection"
                        v-model.trim="detectionAddress"
                        type="text"
                        inputmode="url"
                        maxlength="2048"
                        autocapitalize="none"
                        spellcheck="false"
                        placeholder="login.company.com or a metadata URL"
                        @input="detectedProvider = undefined"
                      />
                      <p class="hint">
                        We check HTTPS, detect OpenID Connect or SAML, and
                        verify your domain's DNS record. Nothing is saved yet.
                      </p>
                      <UtilsButton
                        type="button"
                        variant="secondary"
                        class="mt-4"
                        :disabled="busy || !detectionAddress"
                        @click="detectProvider"
                        >{{
                          busy ? "Checking provider…" : "Detect configuration"
                        }}</UtilsButton
                      >
                      <div
                        v-if="detectedProvider"
                        class="mt-5 rounded-xl border border-amber-200 bg-amber-50/50 p-5"
                        role="region"
                        aria-label="Detected provider"
                      >
                        <h3>
                          {{
                            detectedProvider.configuration.protocol === "oidc"
                              ? "OpenID Connect provider found"
                              : "SAML provider found"
                          }}
                        </h3>
                        <dl class="mt-3 space-y-2 text-sm">
                          <dt class="text-zinc-500">Provider identity</dt>
                          <dd class="break-all">
                            {{
                              detectedProvider.configuration.issuer ||
                              detectedProvider.configuration.entity_id
                            }}
                          </dd>
                          <dt class="text-zinc-500">Company domain</dt>
                          <dd>
                            {{ detectedProvider.domain }} ·
                            {{
                              detectedProvider.domain_verified
                                ? "Ownership verified"
                                : "DNS verification still pending"
                            }}
                          </dd>
                          <template
                            v-if="
                              detectedProvider.configuration.protocol === 'oidc'
                            "
                            ><dt class="text-zinc-500">Application setup</dt>
                            <dd>
                              {{
                                detectedProvider.configuration
                                  .automatic_registration
                                  ? "Automatic registration is available; your provider may require an initial token."
                                  : "Your IT team must supply an application client ID and secret."
                              }}
                            </dd></template
                          >
                          <template v-else
                            ><dt class="text-zinc-500">Signing certificates</dt>
                            <dd>
                              {{
                                detectedProvider.configuration
                                  .signing_certificates
                              }}
                              current certificate(s) found
                            </dd></template
                          >
                        </dl>
                        <p class="hint">
                          Fill in the detected settings? You can review them
                          before saving. A test sign-in is still required to
                          confirm the complete connection.
                        </p>
                        <div class="mt-4 flex flex-wrap gap-3">
                          <UtilsButton
                            type="button"
                            @click="useDetectedProvider"
                            >Use detected settings</UtilsButton
                          ><UtilsButton
                            type="button"
                            variant="secondary"
                            @click="detectedProvider = undefined"
                            >Keep manual setup</UtilsButton
                          >
                        </div>
                      </div>
                      <details
                        :open="prefilled"
                        class="mt-6 rounded-xl border border-zinc-200 p-4"
                      >
                        <summary
                          class="cursor-pointer text-sm font-medium text-zinc-700"
                        >
                          {{
                            prefilled
                              ? "Review detected settings"
                              : "Enter settings manually"
                          }}
                        </summary>
                        <label for="sso-protocol">Connection type</label
                        ><select id="sso-protocol" v-model="protocol">
                          <option value="oidc">
                            Microsoft, Google, Okta or another OpenID provider
                          </option>
                          <option value="saml">
                            SAML · configured by your IT team
                          </option></select
                        ><template v-if="protocol === 'oidc'"
                          ><label for="sso-issuer"
                            >Company sign-in address</label
                          ><input
                            id="sso-issuer"
                            v-model="issuer"
                            type="text"
                            inputmode="url"
                            autocapitalize="none"
                            spellcheck="false"
                            required
                            maxlength="2048"
                            placeholder="login.company.com"
                            :aria-invalid="Boolean(issuerError)"
                            aria-describedby="sso-issuer-hint"
                            @blur="issuer && normalizeIssuerInput()" />
                          <p
                            id="sso-issuer-hint"
                            class="hint"
                            :role="issuerError ? 'alert' : undefined"
                          >
                            {{
                              issuerError ||
                              "Paste a sign-in or OpenID discovery address. We add HTTPS when missing and discover public keys automatically."
                            }}
                          </p>
                          <details
                            class="mt-5 rounded-xl border border-zinc-200 p-4"
                          >
                            <summary
                              class="cursor-pointer text-sm font-medium text-zinc-700"
                            >
                              My provider requires app credentials
                            </summary>
                            <p class="hint">
                              Leave these empty if your provider supports
                              automatic registration or this connection already
                              exists. Secrets are encrypted and cannot be read
                              back.
                            </p>
                            <label for="sso-client">Application client ID</label
                            ><input
                              id="sso-client"
                              v-model.trim="clientId"
                              maxlength="2048"
                              autocomplete="off"
                            /><label for="sso-secret"
                              >Application client secret</label
                            ><input
                              id="sso-secret"
                              v-model="secret"
                              type="password"
                              maxlength="4096"
                              autocomplete="new-password"
                            /><label for="sso-registration"
                              >Initial registration token · optional</label
                            ><input
                              id="sso-registration"
                              v-model="registrationToken"
                              type="password"
                              maxlength="4096"
                              autocomplete="new-password"
                            /></details></template
                        ><template v-else
                          ><label for="sso-metadata"
                            >Provider configuration (metadata XML)</label
                          ><textarea
                            id="sso-metadata"
                            v-model="metadata"
                            required
                            maxlength="131072"
                            rows="7"
                            spellcheck="false"
                          />
                          <p class="hint">
                            Paste the metadata document supplied by your IT
                            team.
                          </p></template
                        >
                      </details>
                      <div class="mt-5 flex gap-3">
                        <UtilsButton
                          type="button"
                          variant="secondary"
                          @click="providerStep = 1"
                          >Back</UtilsButton
                        ><UtilsButton
                          type="button"
                          :disabled="
                            protocol === 'oidc'
                              ? !providerAddressValid
                              : !metadata.trim()
                          "
                          @click="reviewConnection"
                          >Review connection</UtilsButton
                        >
                      </div>
                    </div>
                    <div v-else>
                      <h3>Ready to connect your team?</h3>
                      <p>
                        We will verify your domain and connect your provider
                        before enabling sign-in.
                      </p>
                      <dl class="my-5 rounded-xl bg-stone-50 p-4 text-sm">
                        <dt class="text-zinc-500">Email domain</dt>
                        <dd class="mt-1 mb-4 font-medium text-zinc-900">
                          {{ domain }}
                        </dd>
                        <dt class="text-zinc-500">Sign-in provider</dt>
                        <dd class="mt-1 break-all text-zinc-900">
                          {{
                            protocol === "oidc"
                              ? issuer
                              : "SAML provider configuration"
                          }}
                        </dd>
                      </dl>
                      <label class="check"
                        ><input v-model="jit" type="checkbox" />Automatically
                        add new colleagues on first sign-in</label
                      >
                      <p class="hint">
                        Off by default. New people receive no permissions
                        automatically. Existing accounts keep their access.
                      </p>
                      <div class="mt-5 flex gap-3">
                        <UtilsButton
                          type="button"
                          variant="secondary"
                          @click="providerStep = 2"
                          >Back</UtilsButton
                        ><UtilsButton type="submit"
                          >Verify and connect</UtilsButton
                        >
                      </div>
                    </div>
                  </fieldset>
                </form>
              </section>
            </template>
            <AdministrationPermissions
              v-else
              :settings="settings"
              :users="users"
              :roles="roles"
              :request="graphql"
              @success="
                (notice: string) => {
                  failed = false;
                  message = notice;
                }
              "
              @failure="
                (notice: string) => {
                  failed = true;
                  message = notice;
                }
              "
              @settings-changed="
                run(
                  refresh,
                  '',
                  'Your change was saved, but the settings could not be refreshed. Reopen this section to load the current settings.',
                )
              "
              @people-changed="
                run(
                  refreshPeople,
                  '',
                  'Your change was saved, but the team list could not be refreshed. Use Reload people to load the current access.',
                )
              "
            />
          </template>
        </div>
      </div>
    </div>
    <UtilsModal
      :is-open="!!selectedUser"
      :title="selectedUser ? `Access for ${label(selectedUser)}` : ''"
      @close="selectedUser = undefined"
    >
      <p
        v-if="message && !pending"
        :role="failed ? 'alert' : 'status'"
        :class="[
          'mb-4 rounded-lg border p-3 text-sm',
          failed
            ? 'border-red-200 bg-red-50 text-red-800'
            : 'border-amber-200 bg-amber-50 text-amber-900',
        ]"
      >
        {{ message }}
      </p>
      <template v-if="selectedUser"
        ><h3 class="text-sm font-semibold text-zinc-900">Roles</h3>
        <p class="mt-1 text-xs text-zinc-500">
          Roles collect the access configured by your organization.
        </p>
        <label for="person-role-search" class="sr-only">Search roles</label
        ><input
          id="person-role-search"
          v-model="roleSearch"
          type="search"
          placeholder="Find a role"
        />
        <label v-for="role in roles" :key="role.roleName" class="check"
          ><input
            type="checkbox"
            :checked="hasRole(selectedUser.userId, role.roleName)"
            :disabled="busy || rolesRefreshing || !selectedUser.isActive"
            @click.prevent="
              changeRole(
                selectedUser,
                role.roleName,
                !hasRole(selectedUser.userId, role.roleName),
              )
            "
          />{{ role.roleName }}</label
        >
        <UtilsButton
          v-if="rolesAfter"
          type="button"
          variant="secondary"
          :disabled="rolesRefreshing || busy"
          @click="
            run(
              () => refreshUserRoles(true),
              '',
              'More roles could not be loaded. Try again.',
            )
          "
          >Load more roles</UtilsButton
        >
        <p
          v-if="!roles.length"
          class="mt-4 rounded-lg bg-stone-50 p-3 text-sm text-zinc-500"
        >
          No roles have been configured yet.
        </p>
        <div class="mt-6 border-t border-zinc-100 pt-4 space-y-3">
          <h3 class="text-sm font-semibold text-zinc-900">Account recovery</h3>
          <p class="text-xs text-zinc-500">
            Reset a lost passkey only after verifying the person's identity.
          </p>
          <UtilsButton
            type="button"
            variant="secondary"
            :disabled="busy || !selectedUser.isActive"
            @click="resetKey(selectedUser)"
            >Reset passkey</UtilsButton
          >
        </div></template
      >
      <template #footer
        ><UtilsButton
          type="button"
          variant="secondary"
          @click="selectedUser = undefined"
          >Done</UtilsButton
        ><UtilsButton
          v-if="selectedUser"
          type="button"
          :variant="selectedUser.isActive ? 'danger' : 'primary'"
          :disabled="busy || selectedUser.userId === account?.subject"
          @click="changeStatus(selectedUser)"
          >{{
            selectedUser.isActive ? "Suspend access" : "Restore access"
          }}</UtilsButton
        ></template
      >
    </UtilsModal>
    <UtilsModal
      :is-open="!!pending"
      :title="pending?.title"
      @close="pending = undefined"
      ><p class="text-sm leading-relaxed text-zinc-600">
        {{ pending?.description }}
      </p>
      <p
        v-if="failed && message"
        role="alert"
        class="mt-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800"
      >
        {{ message }}
      </p>
      <template #footer
        ><UtilsButton
          type="button"
          variant="secondary"
          :disabled="busy"
          @click="pending = undefined"
          >Cancel</UtilsButton
        ><UtilsButton type="button" :disabled="busy" @click="applyConfirmed">{{
          busy ? "Applying…" : "Confirm"
        }}</UtilsButton></template
      ></UtilsModal
    >
  </section>
</template>

<style scoped>
.admin-card {
  border: 1px solid #e4e4e7;
  border-radius: 1rem;
  padding: 1.5rem;
  background: white;
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.025);
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
fieldset {
  min-width: 0;
}
fieldset:disabled {
  opacity: 0.55;
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
p.hint {
  font-size: 0.75rem;
}
</style>
