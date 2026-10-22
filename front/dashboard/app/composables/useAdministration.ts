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

export function useAdministration() {
  const { t } = useI18n();
  function formatFailure(error: unknown, fallback: string) {
    return administrationError(
      error,
      fallback,
      t("administration.service_version_mismatch"),
    );
  }

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
        if (preview.domain !== requestedDomain)
          throw new Error("Domain changed");
        providerFormValues(preview.configuration);
        detectedProvider.value = preview;
      },
      "",
      t("administration.copy.we_couldn_t_verify_a_provider_at_this"),
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
    message.value = t(
      "administration.copy.detected_settings_filled_in_review_them_below_before",
    );
  }
  function normalizeDomainInput() {
    try {
      domain.value = normalizeCompanyDomain(domain.value);
      domainError.value = "";
      return true;
    } catch {
      domainError.value = t(
        "administration.copy.enter_a_company_domain_or_work_email_such",
      );
      return false;
    }
  }
  function normalizeIssuerInput() {
    try {
      issuer.value = normalizeProviderAddress(issuer.value);
      issuerError.value = "";
      return true;
    } catch {
      issuerError.value = t(
        "administration.copy.enter_your_provider_s_https_address_without_a",
      );
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
  const roles = ref<{ roleName: string; displayName?: string | null }[]>([]);
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
        roles: DirectoryPage<{
          roleName: string;
          displayName?: string | null;
        }> & {
          assignedRoles: string[];
        };
      }>(
        "query($search: String, $after: String, $subject: String!) { administration { roles(search: $search, after: $after, limit: 20) { items { roleName displayName } after assignedRoles(userId: $subject) } } }",
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
      t("administration.copy.this_person_s_access_could_not_be_loaded"),
    );
  }
  watch(
    userSearch,
    useDebounceFn(
      () =>
        refreshPeople().catch((error) => {
          failed.value = true;
          message.value = formatFailure(
            error,
            t("administration.copy.your_team_could_not_be_loaded_use_reload"),
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
          message.value = formatFailure(
            error,
            t(
              "administration.copy.roles_could_not_be_loaded_retry_your_search",
            ),
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
  async function renamePerson(name: string) {
    const person = selectedUser.value;
    if (!person) return;
    await run(async () => {
      await graphql(
        'mutation($id: String!, $name: String!) { administration { setDirectoryDisplayName(subjectType: "user", subjectId: $id, displayName: $name) } }',
        { id: person.userId, name },
      );
      selectedUser.value = { ...person, displayName: name };
      await refreshPeople();
    }, t("administration.display_name_saved"));
  }
  function changeRole(user: Member, role: string, enabled: boolean) {
    confirm(
      enabled
        ? t("administration.copy.grant_this_role")
        : t("administration.copy.remove_this_role"),
      t(
        enabled
          ? "administration.assign_role_notice"
          : "administration.remove_role_notice",
        { person: label(user), role },
      ),
      async () => {
        await graphql(
          enabled
            ? "mutation($user: String!, $role: String!) { administration { assignRoleToUser(userId: $user, roleName: $role) } }"
            : "mutation($user: String!, $role: String!) { administration { unassignRoleFromUser(userId: $user, roleName: $role) } }",
          { user: user.userId, role },
        );
        await refreshUserRoles();
      },
      t("administration.copy.access_updated"),
    );
  }
  function changeStatus(user: Member) {
    confirm(
      user.isActive
        ? t("administration.copy.suspend_this_person")
        : t("administration.copy.restore_access_2"),
      user.isActive
        ? t("administration.copy.value0_will_no_longer_be_able_to_use", {
            value0: label(user),
          })
        : t("administration.copy.value0_will_be_able_to_sign_in_again", {
            value0: label(user),
          }),
      async () => {
        await graphql(
          "mutation($user: String!, $active: Boolean!) { administration { updateUser(userId: $user, isActive: $active) } }",
          { user: user.userId, active: !user.isActive },
        );
        await refreshPeople();
      },
      user.isActive
        ? t("administration.copy.access_suspended")
        : t("administration.copy.access_restored"),
    );
  }
  function resetKey(user: Member) {
    confirm(
      t("administration.copy.reset_this_person_s_passkey"),
      t("administration.copy.value0_must_create_a_new_passkey_after_their", {
        value0: label(user),
      }),
      async () => {
        await graphql(
          "mutation($user: String!) { administration { resetUserPasskeys(userId: $user) } }",
          { user: user.userId },
        );
      },
      t("administration.copy.passkey_reset_a_new_key_will_be_requested"),
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
      t("administration.copy.organization_saved"),
      t("administration.copy.your_organization_name_could_not_be_saved_use"),
    );
  }
  function changeMfa(enabled: boolean) {
    confirm(
      enabled
        ? t("administration.copy.require_a_passkey_for_your_team")
        : t("administration.copy.turn_off_the_passkey_requirement"),
      enabled
        ? t("administration.copy.everyone_will_need_a_passkey_after_signing_in")
        : t("administration.copy.your_team_will_continue_to_sign_in_using"),
      async () => {
        await graphql(
          "mutation($enabled: Boolean!) { administration { setPasskeyMfa(enabled: $enabled) } }",
          { enabled },
        );
        if (enabled) await useSessionState().requirePasskey();
        await refresh();
      },
      t("administration.copy.sign_in_security_updated"),
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
      message.value = formatFailure(error, failureMessage);
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
    }, t("administration.copy.publish_this_dns_txt_record_then_save_your"));
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
    }, t("administration.copy.connection_saved_your_verified_email_domain_now_selects"));
  }
  function disable(domain: string) {
    confirm(
      t("administration.copy.disconnect_company_sign_in"),
      t("administration.copy.new_sign_ins_for_value0_will_stop_existing", {
        value0: domain,
      }),
      async () => {
        await graphql(
          "mutation($domain: String!) { administration { disableSsoConnection(domain: $domain) } }",
          { domain },
        );
        await refresh();
      },
      t("administration.copy.new_sign_ins_are_disabled_for_this_domain"),
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
    }, t("administration.copy.external_identity_linked_to_the_existing_account"));
  }
  onMounted(() =>
    run(async () => {
      await refresh();
      await refreshPeople();
    }, ""),
  );
  return {
    settings,
    challenge,
    busy,
    message,
    failed,
    domain,
    protocol,
    issuer,
    domainError,
    issuerError,
    detectionAddress,
    prefilled,
    detectedProvider,
    detectProvider,
    useDetectedProvider,
    normalizeDomainInput,
    normalizeIssuerInput,
    providerAddressValid,
    reviewConnection,
    clientId,
    secret,
    registrationToken,
    metadata,
    jit,
    linkDomain,
    externalSubject,
    userId,
    organizationName,
    providerStep,
    userSearch,
    users,
    peopleLoaded,
    peopleRefreshing,
    roles,
    assignedRoles,
    peopleAfter,
    rolesAfter,
    roleSearch,
    rolesRefreshing,
    peopleEpoch,
    rolesEpoch,
    selectedUser,
    account,
    visibleUsers,
    pending,
    confirm,
    applyConfirmed,
    refreshPeople,
    refreshUserRoles,
    openUser,
    label,
    hasRole,
    changeRole,
    renamePerson,
    changeStatus,
    resetKey,
    saveOrganization,
    changeMfa,
    graphql,
    refresh,
    run,
    requestDomain,
    saveConnection,
    disable,
    linkIdentity,
  };
}
