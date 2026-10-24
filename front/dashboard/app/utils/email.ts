/** Connection views deliberately contain no credential values. */
export const emailRights = [
  "view",
  "read",
  "use",
  "edit",
  "delete",
  "reveal",
  "share",
  "send",
  "approve",
  "automate",
] as const;
export type EmailRight = (typeof emailRights)[number];
export interface EmailConfig {
  name: string;
  address: string;
  imap_host: string;
  imap_port: 993;
  smtp_host: string;
  smtp_port: 465 | 587;
  folders: string[];
  enabled: boolean;
  ingest: boolean;
  poll_seconds: number;
  initial_sync: "new" | "all";
}
export interface EmailAccount {
  id: string;
  revision: number;
  config: EmailConfig;
  rights: EmailRight[];
  has_credentials: boolean;
  sync_status: string;
  last_sync: number;
  can_manage_access: boolean;
}
export interface EmailAttachment {
  name: string;
  content_type: string;
  content: string;
}
export interface EmailMessage {
  id: string;
  sequence: number;
  label: string;
  folder: string;
  message: {
    message_id: string;
    sender: string;
    recipients: string[];
    subject: string;
    date: string;
    body: string;
    references: string;
    reply_allowed: boolean;
    attachments: EmailAttachment[];
  };
}
export interface EmailDraftContent {
  to: string[];
  cc: string[];
  subject: string;
  body: string;
  reply_to_message_id: string;
  references: string;
  attachments: EmailAttachment[];
}
export interface EmailDraft {
  id: string;
  revision: number;
  status: string;
  content: EmailDraftContent;
  delivery_id: string;
  automatic: boolean;
}
export interface EmailProfile {
  model: string;
  fallbacks: string[];
  prompt: string;
  skill_ids: string[];
  language: string;
  tone: string;
  signature: string;
  mode: "draft" | "approval" | "automatic";
  allowed_senders: string[];
  daily_limit: number;
  sender_daily_limit: number | null;
  one_reply_per_thread: boolean;
}

export function decoded(value: unknown): unknown {
  return typeof value === "string" ? JSON.parse(value) : value;
}
export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("EMAIL_RESPONSE_INVALID");
  return Object.fromEntries(Object.entries(value));
}
export function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("EMAIL_RESPONSE_INVALID");
  return value;
}
export function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new Error("EMAIL_RESPONSE_INVALID");
  return value;
}
function flag(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("EMAIL_RESPONSE_INVALID");
  return value;
}
export function texts(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error("EMAIL_RESPONSE_INVALID");
  return value.map(text);
}
export function rows(value: unknown): unknown[] {
  const result = decoded(value);
  if (!Array.isArray(result)) throw new Error("EMAIL_RESPONSE_INVALID");
  return result;
}
export function parseAccounts(value: unknown): EmailAccount[] {
  return rows(value).map((item) => {
    const row = object(item),
      config = object(row.config),
      rights = texts(row.rights);
    if (
      Object.keys(row).some((key) =>
        /secret|password|username|token/i.test(key),
      ) ||
      "credentials" in row ||
      Object.keys(config).some((key) =>
        /secret|password|username|token|credentials/i.test(key),
      )
    )
      throw new Error("EMAIL_RESPONSE_INVALID");
    const validated: EmailRight[] = [];
    for (const right of rights) {
      const known = emailRights.find((candidate) => candidate === right);
      if (!known) throw new Error("EMAIL_RESPONSE_INVALID");
      validated.push(known);
    }
    const imap = number(config.imap_port),
      smtp = number(config.smtp_port);
    if (imap !== 993 || (smtp !== 465 && smtp !== 587))
      throw new Error("EMAIL_RESPONSE_INVALID");
    const initial = config.initial_sync ?? "new";
    if (initial !== "new" && initial !== "all")
      throw new Error("EMAIL_RESPONSE_INVALID");
    return {
      can_manage_access:
        row.can_manage_access === undefined
          ? false
          : flag(row.can_manage_access),
      id: text(row.id),
      revision: number(row.revision),
      rights: validated,
      has_credentials: flag(row.has_credentials),
      sync_status: text(row.sync_status),
      last_sync: number(row.last_sync),
      config: {
        name: text(config.name),
        address: text(config.address),
        imap_host: text(config.imap_host),
        imap_port: imap,
        smtp_host: text(config.smtp_host),
        smtp_port: smtp,
        folders: texts(config.folders),
        enabled: flag(config.enabled),
        ingest: flag(config.ingest),
        poll_seconds: number(config.poll_seconds),
        initial_sync: initial,
      },
    };
  });
}
export function can(account: EmailAccount, right: EmailRight): boolean {
  return account.rights.includes(right);
}
export function connectionUpdate(
  account: EmailAccount,
  username: string,
  password: string,
) {
  if (Boolean(username) !== Boolean(password))
    throw new Error("EMAIL_CREDENTIALS_INCOMPLETE");
  return {
    operation: "update",
    account: account.id,
    revision: account.revision,
    config: account.config,
    ...(username && password ? { credentials: { username, password } } : {}),
  };
}
export function attachments(value: unknown): EmailAttachment[] {
  if (!Array.isArray(value)) throw new Error("EMAIL_RESPONSE_INVALID");
  return value.map((item) => {
    const row = object(item);
    return {
      name: text(row.name),
      content_type: text(row.content_type),
      content: text(row.content),
    };
  });
}
export function parseMessages(value: unknown): EmailMessage[] {
  return rows(value).map((item) => {
    const row = object(item),
      message = object(row.message);
    return {
      id: text(row.id),
      sequence: number(row.sequence),
      label: text(row.label),
      folder: text(row.folder),
      message: {
        message_id: text(message.message_id),
        sender: text(message.sender),
        recipients: texts(message.recipients),
        subject: text(message.subject),
        date: text(message.date),
        body: text(message.body),
        references: text(message.references),
        reply_allowed: flag(message.reply_allowed),
        attachments: attachments(message.attachments),
      },
    };
  });
}
export function parseMessagePage(value: unknown): {
  items: EmailMessage[];
  next_before: number;
} {
  const data = decoded(value);
  if (Array.isArray(data))
    return { items: parseMessages(data), next_before: 0 };
  const page = object(data);
  return {
    items: parseMessages(page.items),
    next_before: number(page.next_before),
  };
}
export function parseDrafts(value: unknown): EmailDraft[] {
  return rows(value).map((item) => {
    const row = object(item),
      content = object(row.content);
    return {
      id: text(row.id),
      revision: number(row.revision),
      status: text(row.status),
      delivery_id: text(row.delivery_id),
      automatic: flag(row.automatic),
      content: {
        to: texts(content.to),
        cc: texts(content.cc),
        subject: text(content.subject),
        body: text(content.body),
        reply_to_message_id: text(content.reply_to_message_id),
        references: text(content.references),
        attachments: attachments(content.attachments),
      },
    };
  });
}
export function parseProfile(value: unknown): EmailProfile | null {
  const result = decoded(value);
  if (result === null) return null;
  const row = object(result),
    mode = text(row.mode);
  if (mode !== "draft" && mode !== "approval" && mode !== "automatic")
    throw new Error("EMAIL_RESPONSE_INVALID");
  return {
    model: text(row.model),
    fallbacks: texts(row.fallbacks),
    prompt: text(row.prompt),
    skill_ids: texts(row.skill_ids),
    language: text(row.language),
    tone: text(row.tone),
    signature: text(row.signature),
    mode,
    allowed_senders: texts(row.allowed_senders),
    daily_limit: number(row.daily_limit),
    sender_daily_limit:
      row.sender_daily_limit === null || row.sender_daily_limit === undefined
        ? null
        : number(row.sender_daily_limit),
    one_reply_per_thread:
      row.one_reply_per_thread === undefined
        ? false
        : flag(row.one_reply_per_thread),
  };
}

export type EmailDiscoveryStatus =
  "found" | "absent" | "unavailable" | "unsupported" | "blocked";
export interface EmailEndpoint {
  host: string;
  port: 993 | 465 | 587;
  security: "tls" | "starttls";
  priority: number;
  weight: number;
}
export interface EmailDiscoveryResult {
  domain: string;
  imap: EmailEndpoint[];
  smtp: EmailEndpoint[];
  imap_status: EmailDiscoveryStatus;
  smtp_status: EmailDiscoveryStatus;
  username?: "email" | "local_part" | "manual";
  oauth_provider?: "google" | "microsoft" | null;
}

export function parseDiscovery(value: unknown): EmailDiscoveryResult {
  const row = object(decoded(value));
  if (
    Object.keys(row).some(
      (key) =>
        ![
          "domain",
          "imap",
          "smtp",
          "imap_status",
          "smtp_status",
          "username",
          "oauth_provider",
        ].includes(key),
    )
  )
    throw new Error("EMAIL_RESPONSE_INVALID");
  const host = (value: unknown) => {
    const name = text(value);
    if (
      name.length > 253 ||
      !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(name) ||
      !name.includes(".")
    )
      throw new Error("EMAIL_RESPONSE_INVALID");
    return name;
  };
  const status = (value: unknown): EmailDiscoveryStatus => {
    if (
      value !== "found" &&
      value !== "absent" &&
      value !== "unavailable" &&
      value !== "unsupported" &&
      value !== "blocked"
    )
      throw new Error("EMAIL_RESPONSE_INVALID");
    return value;
  };
  const endpoints = (value: unknown, incoming: boolean): EmailEndpoint[] => {
    if (!Array.isArray(value) || value.length > (incoming ? 32 : 64))
      throw new Error("EMAIL_RESPONSE_INVALID");
    return value.map((item) => {
      const entry = object(item),
        port = number(entry.port),
        security = entry.security;
      if (
        incoming
          ? port !== 993 || security !== "tls"
          : !(
              (port === 465 && security === "tls") ||
              (port === 587 && security === "starttls")
            )
      )
        throw new Error("EMAIL_RESPONSE_INVALID");
      if (
        (port !== 993 && port !== 465 && port !== 587) ||
        (security !== "tls" && security !== "starttls")
      )
        throw new Error("EMAIL_RESPONSE_INVALID");
      const priority = number(entry.priority),
        weight = number(entry.weight);
      if (
        !Number.isInteger(priority) ||
        !Number.isInteger(weight) ||
        priority < 0 ||
        priority > 65535 ||
        weight < 0 ||
        weight > 65535
      )
        throw new Error("EMAIL_RESPONSE_INVALID");
      return { host: host(entry.host), port, security, priority, weight };
    });
  };
  const imap = endpoints(row.imap, true),
    smtp = endpoints(row.smtp, false),
    imap_status = status(row.imap_status),
    smtp_status = status(row.smtp_status);
  if (
    imap.length > 0 !== (imap_status === "found") ||
    smtp.length > 0 !== (smtp_status === "found")
  )
    throw new Error("EMAIL_RESPONSE_INVALID");
  const username = row.username ?? "email";
  const oauth_provider = row.oauth_provider ?? null;
  if (
    (username !== "email" &&
      username !== "local_part" &&
      username !== "manual") ||
    (oauth_provider !== null &&
      oauth_provider !== "google" &&
      oauth_provider !== "microsoft")
  )
    throw new Error("EMAIL_RESPONSE_INVALID");
  return {
    domain: host(row.domain),
    imap,
    smtp,
    imap_status,
    smtp_status,
    username,
    oauth_provider,
  };
}
