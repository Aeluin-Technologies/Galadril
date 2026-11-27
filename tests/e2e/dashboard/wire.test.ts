import assert from "node:assert/strict";
import test from "node:test";
import { frame, operation, record, stringField } from "./wire.ts";

await test("GraphQL fixtures require query text and object variables", () => {
  assert.deepEqual(operation({ query: "query { value }" }), {
    query: "query { value }",
    variables: {},
  });
  assert.throws(() => operation({ query: [] }), /query/);
  assert.throws(
    () => operation({ query: "query { value }", variables: [] }),
    /object/,
  );
});

await test("Fixture objects and strings retain their runtime contract", () => {
  assert.equal(stringField(record({ cursor: "next" }).cursor), "next");
  assert.throws(() => record(null), /object/);
  assert.throws(() => stringField(1), /string/);
});

await test("subscription frames require identifiers only when subscribing", () => {
  assert.deepEqual(frame('{"type":"connection_init"}'), {
    type: "connection_init",
  });
  assert.deepEqual(
    frame('{"type":"subscribe","id":"1","payload":{"query":"test"}}'),
    {
      type: "subscribe",
      id: "1",
      payload: { query: "test" },
    },
  );
  assert.throws(() => frame('{"type":"subscribe"}'), /string/);
});
