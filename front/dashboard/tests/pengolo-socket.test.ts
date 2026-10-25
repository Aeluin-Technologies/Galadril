import assert from "node:assert/strict";
import { test } from "node:test";
import { createPengoloSocket } from "../app/utils/pengolo-socket.ts";

class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState: 0 | 1 | 2 | 3 = 1;
  sent: Array<Record<string, unknown>> = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  constructor() {
    FakeSocket.instances.push(this);
  }
  send(value: string) {
    this.sent.push(JSON.parse(value));
  }
  close() {
    this.onclose?.();
  }
  receive(value: object) {
    this.onmessage?.({ data: JSON.stringify(value) });
  }
}

test("one socket multiplexes identity and generation, then reconnects with the latest cursor", () => {
  const transport = createPengoloSocket(
    "wss://example.test/graphql",
    FakeSocket,
  );
  let cursor = 0;
  const received: unknown[] = [];
  const stopIdentity = transport.subscribe(
    "identity",
    () => ({ query: "subscription Identity { identity }" }),
    (value) => received.push(value),
    () => {},
  );
  const stopRun = transport.subscribe(
    "run",
    () => ({
      query: "subscription Run { events }",
      variables: { after: cursor },
    }),
    (value) => {
      cursor = 2;
      received.push(value);
    },
    () => {},
  );
  assert.equal(FakeSocket.instances.length, 1);
  const socket = FakeSocket.instances.at(-1)!;
  socket.onopen?.();
  socket.receive({ type: "connection_ack" });
  assert.deepEqual(
    socket.sent
      .filter((frame) => frame.type === "subscribe")
      .map((frame) => frame.id),
    ["identity", "run"],
  );
  socket.receive({
    type: "next",
    id: "run",
    payload: { data: { events: [1, 2] } },
  });
  assert.equal(received.length, 1);
  transport.reconnect();
  const reconnected = FakeSocket.instances.at(-1)!;
  reconnected.onopen?.();
  reconnected.receive({ type: "connection_ack" });
  const run = reconnected.sent.find((frame) => frame.id === "run");
  assert.equal(
    (run?.payload as { variables: { after: number } }).variables.after,
    2,
  );
  stopRun();
  stopIdentity();
  transport.clear();
});
