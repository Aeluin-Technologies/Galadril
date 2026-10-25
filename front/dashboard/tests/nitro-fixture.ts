import { IncomingMessage, ServerResponse } from "node:http";
import { Socket } from "node:net";
import { createEvent } from "h3";

/** Native H3 events keep middleware fixtures aligned with the production handler contract. */
export function testEvent(method: "GET" | "POST" = "GET") {
  const request = new IncomingMessage(new Socket());
  request.method = method;
  return createEvent(request, new ServerResponse(request));
}
