import { usePengoloWorkspace } from "~/stores/usePengoloWorkspace";

export interface AccountSession {
  subject: string;
  tenant: string;
  displayName?: string;
  expiresAt?: number;
  requiresPasskey?: boolean;
}

export function useAccount() {
  const session = useState<AccountSession | null>(
    "account-session",
    () => null,
  );
  const administrator = useState("account-administrator", () => false);
  const request = useRequestFetch();

  async function refreshAccount() {
    administrator.value = false;
    if (!session.value)
      session.value = await request<AccountSession>("/auth/session");
    if (
      import.meta.client &&
      useRouter().currentRoute.value.path.startsWith("/pengolo")
    ) {
      try {
        await usePengoloWorkspace().load();
        administrator.value =
          usePengoloWorkspace().identity?.administrator === true;
      } catch {
        administrator.value = false;
      }
      return;
    }
    try {
      const response = await request<{
        data?: {
          administration?: { ssoSettings?: { organizationName: string } };
        };
        errors?: unknown[];
      }>("/graphql", {
        method: "POST",
        body: {
          query:
            "query { administration { ssoSettings { organizationName } } }",
        },
      });
      administrator.value =
        !response.errors?.length &&
        typeof response.data?.administration?.ssoSettings?.organizationName ===
          "string";
    } catch {
      administrator.value = false;
    }
  }

  return { session, administrator, refreshAccount };
}
