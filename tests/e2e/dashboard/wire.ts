function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Parse an expected fixture boundary before assertions read nested fields. */
export function record(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error("Expected a JSON object in the browser fixture");
  }
  return value;
}

export function stringField(value: unknown): string {
  if (typeof value !== "string")
    throw new Error("Expected a string in the browser fixture");
  return value;
}

export function operation(value: unknown): {
  query: string;
  variables: Record<string, unknown>;
} {
  const request = record(value);
  if (typeof request.query !== "string")
    throw new Error("The GraphQL fixture request needs query text");
  const variables =
    request.variables === undefined ? {} : record(request.variables);
  return { query: request.query, variables };
}

/** Decode the subset of the GraphQL transport contract answered by the fixture. */
export function frame(text: string): {
  type: string;
  id?: string;
  payload?: { query?: string };
} {
  const request = record(JSON.parse(text));
  const type = stringField(request.type);
  if (type !== "subscribe") return { type };

  const id = stringField(request.id);
  if (request.payload === undefined) return { type, id };

  const query = record(request.payload).query;
  return {
    type,
    id,
    payload: query === undefined ? {} : { query: stringField(query) },
  };
}

/** Validate array members without propagating JSON's unchecked element type. */
export function stringList(value: unknown): string[] {
  if (!Array.isArray(value))
    throw new Error("Expected an array in the browser fixture");
  return value.map(stringField);
}
