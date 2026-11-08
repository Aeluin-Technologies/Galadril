import { authConfig } from "../../../auth/config.ts";
import { ensureMcpClient, mcpAuthorization } from "../../../auth/mcp.ts";

export default defineEventHandler(async (event) => {
  setResponseHeader(event, "Cache-Control", "no-store");
  const config = authConfig();
  const { client, parameters } = mcpAuthorization(
    getRequestURL(event).searchParams,
    config.mcpClients ?? [],
  );
  await ensureMcpClient(client, config.admin);
  const target = new URL("/oauth2/auth", config.origin);
  target.search = parameters.toString();
  return sendRedirect(event, target.href, 303);
});
