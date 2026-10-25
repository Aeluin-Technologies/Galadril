/** Unknown external payloads stay untrusted until individual fields are validated. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Callers validate required fields; malformed optional envelopes expose no properties. */
export function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

/** Normalize collection envelopes without assigning unchecked field types to their entries. */
export function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map((item: unknown) => record(item)) : [];
}

/** Required protocol strings must fail closed instead of becoming "undefined" on the wire. */
export function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("Invalid protocol string");
  return value;
}

/** Private protocol strings remain untrusted after JSON decoding. */
export function parseObject(value: unknown): Record<string, unknown> {
  return record(JSON.parse(text(value)));
}
