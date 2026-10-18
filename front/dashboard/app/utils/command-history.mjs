/** Account-bound query history avoids retaining content or leaking another account's searches. */
const maximumBytes = 8192;
const encoder = new TextEncoder();

function identity(scope) {
  if (
    !scope ||
    typeof scope.tenant !== "string" ||
    typeof scope.subject !== "string" ||
    !scope.tenant ||
    !scope.subject ||
    scope.tenant.length > 256 ||
    scope.subject.length > 256
  )
    return null;
  return JSON.stringify([scope.tenant, scope.subject]);
}

function queries(values) {
  if (!Array.isArray(values)) return [];
  const found = [];
  for (const value of values.slice(0, 32)) {
    if (
      typeof value !== "string" ||
      value.length > 512 ||
      /\p{Control}/u.test(value)
    )
      continue;
    const query = value.trim();
    if (query && !found.includes(query)) found.push(query);
    if (found.length === 5) break;
  }
  return found;
}

export function readCommandHistory(source, scope) {
  const account = identity(scope);
  if (
    !account ||
    typeof source !== "string" ||
    source.length > maximumBytes ||
    encoder.encode(source).byteLength > maximumBytes
  )
    return [];
  try {
    const value = JSON.parse(source);
    return value.version === 1 && value.account === account
      ? queries(value.queries)
      : [];
  } catch {
    return [];
  }
}

export function writeCommandHistory(values, scope) {
  const account = identity(scope);
  if (!account) return "";
  const selected = queries(values);
  let encoded = JSON.stringify({ version: 1, account, queries: selected });
  while (encoder.encode(encoded).byteLength > maximumBytes && selected.length) {
    selected.pop();
    encoded = JSON.stringify({ version: 1, account, queries: selected });
  }
  return encoded;
}
