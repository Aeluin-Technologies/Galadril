import { GatewayAuthClient } from "./gateway.mjs";
import { trustedKeys } from "./tokens.mjs";
import { PasskeyService } from "./passkey.mjs";
import { GatewayPasskeyStore } from "./passkey-store.mjs";

let configuration;

/** Fail closed when deployment secrets or trusted endpoints are missing. */
export function authConfig() {
  if (configuration) return configuration;
  const required = (name) => {
    const value = process.env[name];
    if (!value) throw new Error(`Missing ${name}`);
    return value;
  };
  const origin = new URL(required("AUTH_PUBLIC_ORIGIN")).origin;
  const issuer = required("AUTH_ISSUER");
  if (!origin.startsWith("https://") || !issuer.startsWith("https://"))
    throw new Error("Authentication requires HTTPS");
  const sessionSecret = required("AUTH_SESSION_SECRET");
  if (sessionSecret.length < 32) throw new Error("Session secret too short");
  configuration = {
    origin,
    issuer,
    sessionSecret,
    clientSecret: required("AUTH_CLIENT_SECRET"),
    hydra: required("AUTH_HYDRA_PUBLIC_URL"),
    admin: required("AUTH_HYDRA_ADMIN_URL"),
    polis: required("AUTH_POLIS_URL"),
    polisApiKey: required("AUTH_POLIS_API_KEY"),
    kratos:
      process.env.AUTH_LOCAL_LOGIN_ENABLED === "true"
        ? required("AUTH_KRATOS_PUBLIC_URL")
        : undefined,
    kratosAdmin:
      process.env.AUTH_LOCAL_LOGIN_ENABLED === "true"
        ? required("AUTH_KRATOS_ADMIN_URL")
        : undefined,
    keys: trustedKeys(required("AUTH_JWKS_URL")),
    directory: new GatewayAuthClient(
      required("AUTH_GATEWAY_URL"),
      required("AUTH_GATEWAY_KEY"),
    ),
  };
  configuration.passkeys = new PasskeyService(
    new GatewayPasskeyStore(configuration.directory),
    origin,
  );
  return configuration;
}
