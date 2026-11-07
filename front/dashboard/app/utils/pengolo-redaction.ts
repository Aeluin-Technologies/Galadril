export const MAX_REDACTION_BYTES = 16 * 1024 * 1024;
const MAX_WIRE_BYTES = 24 * 1024 * 1024;
export type RedactionRule = {
  kind: "email" | "phone" | "literal" | "regex";
  pattern?: string;
  replacement_mode?: "initials";
};
export interface RedactionOptions {
  emails: boolean;
  phones: boolean;
  addresses: boolean;
  names: string;
  pattern: string;
}
export interface RedactedDocument {
  blob: Blob;
  pages: number;
  matches: number;
  annotationsRemoved: boolean;
}

export function buildRedactionRules(
  options: RedactionOptions,
): RedactionRule[] {
  const rules: RedactionRule[] = [];
  if (options.emails) rules.push({ kind: "email" });
  if (options.phones) rules.push({ kind: "phone" });
  if (options.addresses)
    rules.push({
      kind: "regex",
      pattern:
        "(?i)\\b[0-9]{1,4}[ \\t]+(?:rue|avenue|boulevard|chemin|allée|impasse|place)[^\\r\\n]{3,90}",
    });
  for (const name of new Set(
    options.names
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean),
  )) {
    if ([...name].length > 512 || /\p{Cc}/u.test(name))
      throw new Error("Invalid name rule");
    rules.push({
      kind: "literal",
      pattern: name,
      replacement_mode: "initials",
    });
  }
  if (options.pattern.trim()) {
    if ([...options.pattern].length > 512)
      throw new Error("Pattern exceeds limit");
    rules.push({ kind: "regex", pattern: options.pattern });
  }
  if (!rules.length || rules.length > 16)
    throw new Error("Select between one and sixteen rules");
  return rules;
}

function encode(bytes: Uint8Array): string {
  const chunks: string[] = [];
  for (let offset = 0; offset < bytes.length; offset += 32768)
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 32768)));
  return btoa(chunks.join(""));
}

async function boundedJson(response: Response): Promise<unknown> {
  if (!response.body) throw new Error("Missing redaction response");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const parts: string[] = [];
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_WIRE_BYTES) {
        await reader.cancel();
        throw new Error("Redaction response exceeds limit");
      }
      parts.push(decoder.decode(value, { stream: true }));
    }
    parts.push(decoder.decode());
    try {
      return JSON.parse(parts.join("")) as unknown;
    } catch {
      throw new Error("Invalid redaction response");
    }
  } finally {
    reader.releaseLock();
  }
}

function resultDocument(value: unknown, mime: string): RedactedDocument {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error("Invalid redaction response");
  const result = value as Record<string, unknown>;
  if (
    result.mime !== mime ||
    result.requires_review !== true ||
    result.layout_preserved !== true ||
    result.coverage !== "provided_rules_only" ||
    typeof result.content_base64 !== "string" ||
    result.content_base64.length > MAX_WIRE_BYTES ||
    !Number.isSafeInteger(result.match_count) ||
    typeof result.match_count !== "number" ||
    result.match_count < 0 ||
    result.match_count > 4096 ||
    !Number.isSafeInteger(result.pages) ||
    typeof result.pages !== "number" ||
    result.pages < 0 ||
    result.pages > 128 ||
    !Array.isArray(result.warnings) ||
    result.warnings.length > 2 ||
    result.warnings.some(
      (warning: unknown) =>
        warning !== "annotations_removed" && warning !== "no_matches",
    )
  )
    throw new Error("Invalid redaction guarantees");
  if (
    (mime === "application/pdf" && result.pages < 1) ||
    (mime === "text/plain" && result.pages !== 0)
  )
    throw new Error("Invalid redaction pagination");
  const binary = atob(result.content_base64);
  if (
    !binary.length ||
    binary.length > MAX_REDACTION_BYTES ||
    (mime === "application/pdf" && !binary.startsWith("%PDF-"))
  )
    throw new Error("Invalid redacted document");
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (mime === "text/plain")
    new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  return {
    blob: new Blob([bytes], { type: mime }),
    pages: result.pages,
    matches: result.match_count,
    annotationsRemoved: result.warnings.includes("annotations_removed"),
  };
}

export async function redactLocalDocument(
  file: File,
  rules: readonly RedactionRule[],
  signal: AbortSignal,
  request: typeof fetch = fetch,
): Promise<RedactedDocument> {
  if (
    !file.size ||
    file.size > MAX_REDACTION_BYTES ||
    !rules.length ||
    rules.length > 16
  )
    throw new Error("Invalid local redaction input");
  const mime =
    file.type === "application/pdf" ||
    (!file.type && file.name.toLowerCase().endsWith(".pdf"))
      ? "application/pdf"
      : file.type === "text/plain" ||
          (!file.type && file.name.toLowerCase().endsWith(".txt"))
        ? "text/plain"
        : null;
  if (!mime) throw new Error("Choose a PDF or plain text file");
  const content = new Uint8Array(await file.arrayBuffer());
  signal.throwIfAborted();
  const response = await request("/pengolo/files/redact", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mime, content_base64: encode(content), rules }),
  });
  if (!response.ok) throw new Error("Native redaction unavailable or refused");
  const value = await boundedJson(response);
  signal.throwIfAborted();
  return resultDocument(value, mime);
}
