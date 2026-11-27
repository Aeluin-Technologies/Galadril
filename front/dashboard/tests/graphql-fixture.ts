import { record, text } from "../shared/records.ts";

/** Parse interception payloads before fixture handlers read query variables. */
export function graphQL(value: unknown): {
  query: string;
  variables: Record<string, unknown>;
} {
  const request = record(value);
  const query = text(request.query);
  const { variables } = request;
  if (variables === undefined || variables === null)
    return { query, variables: {} };
  if (typeof variables !== "object" || Array.isArray(variables))
    throw new Error("Expected an object for GraphQL fixture variables");

  return { query, variables: record(variables) };
}
