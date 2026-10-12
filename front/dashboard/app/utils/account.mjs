/** Derive display initials without treating profile text as an identity or role. */
export function accountInitials(label = "") {
  const words = label
    .split("@")[0]
    .trim()
    .split(/[\s._-]+/u)
    .filter(Boolean);
  if (!words.length) return "?";
  return (
    words.length > 1
      ? `${Array.from(words[0])[0]}${Array.from(words.at(-1))[0]}`
      : Array.from(words[0]).slice(0, 2).join("")
  ).toLocaleUpperCase();
}

/** Gateway remains the authority for every administrative read and mutation. */
export function administrationActions(authorized) {
  if (authorized !== true) return [];
  return [
    {
      label: "Organization settings",
      description: "Manage your organization and access",
      href: "/settings/sso",
    },
    {
      label: "Manage people",
      description: "Manage members, roles and account recovery",
      href: "/settings/sso#users",
    },
    {
      label: "Configure single sign-on",
      description: "Verify domains and configure identity providers",
      href: "/settings/sso#domains",
    },
    {
      label: "Manage permissions",
      description: "Manage roles, data access and general access rules",
      href: "/settings/sso#permissions",
    },
  ];
}
