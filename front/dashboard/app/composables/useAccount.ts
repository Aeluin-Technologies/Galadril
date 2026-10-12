interface AccountSession {
  subject: string;
  tenant: string;
  displayName?: string;
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
    session.value = await request<AccountSession>("/auth/session");
    try {
      const response = await request<{
        data?: { administration?: { ssoSettings?: string } };
        errors?: unknown[];
      }>("/graphql", {
        method: "POST",
        body: { query: "query { administration { ssoSettings } }" },
      });
      administrator.value =
        !response.errors?.length &&
        typeof response.data?.administration?.ssoSettings === "string";
    } catch {
      administrator.value = false;
    }
  }

  return { session, administrator, refreshAccount };
}
