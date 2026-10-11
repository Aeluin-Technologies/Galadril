import { createHash, randomBytes } from "node:crypto";
import { jwtVerify } from "jose";
import { metrics } from "@opentelemetry/api";
import { authConfig } from "../../auth/config.mjs";
import {
  activeUser,
  findConnection,
  findLoginProvider,
  resolveIdentity,
} from "../../auth/directory.mjs";
import {
  constantEqual,
  emailDomain,
  externalSubject,
  requireOrigin,
  safeRedirect,
  validateConsent,
} from "../../auth/policy.mjs";
import { verifyAccessToken } from "../../auth/tokens.mjs";
import {
  adminRequest,
  requestJson,
  connectionCredential,
} from "../../auth/http.mjs";
import { passkeyAssurance } from "../../auth/passkey.mjs";
import { ensureClient } from "../../auth/client.mjs";
import { identityCookie, localIdentity } from "../../auth/local.mjs";

const tokenCookie = "__Host-galadril-token";
const cookie = {
  httpOnly: true,
  secure: true,
  sameSite: "lax" as const,
  path: "/",
};
const random = () => randomBytes(32).toString("base64url");
const operations = new Set([
  "session",
  "mfa/options",
  "mfa/verify",
  "local/flow",
  "local/callback",
  "logout",
  "start",
  "login",
  "polis/callback",
  "consent",
  "callback",
]);
const attempts = metrics
  .getMeter("galadril.studio.auth")
  .createCounter("galadril.auth.attempts");
const challenge = (value: string) =>
  createHash("sha256").update(value).digest("base64url");

export default defineEventHandler(async (event) => {
  let outcome = "success";
  let operation = "unknown";
  try {
    setResponseHeader(event, "Cache-Control", "no-store");
    setResponseHeader(event, "Referrer-Policy", "no-referrer");
    const config = authConfig();
    const path = getRouterParam(event, "path");
    operation = operations.has(path ?? "") ? path! : "unknown";
    const query = getQuery(event);
    const param = (name: string) => {
      const value = query[name];
      if (typeof value !== "string" || !value.length || value.length > 2048)
        throw new Error("Invalid parameter");
      return value;
    };
    if (event.method === "POST")
      requireOrigin(getHeader(event, "origin"), config.origin);
    else if (event.method !== "GET") throw new Error("Invalid method");
    if (path === "session" && event.method === "GET") {
      const token = getCookie(event, tokenCookie);
      if (!token) throw new Error("Missing session");
      const claims = await verifyAccessToken(token, config.issuer, config.keys);
      await activeUser(config.directory, claims.tenant_id, claims.sub);
      await config.passkeys.check(claims);
      const profile = await useSession(event, {
        name: "__Host-galadril-profile",
        password: config.sessionSecret,
        cookie,
      });
      return {
        subject: claims.sub,
        tenant: claims.tenant_id,
        expiresAt: claims.exp,
        displayName:
          profile.data.subject === claims.sub &&
          profile.data.tenant === claims.tenant_id
            ? profile.data.displayName
            : undefined,
      };
    }
    const session = await useSession(event, {
      name: "__Host-galadril-flow",
      password: config.sessionSecret,
      maxAge: 600,
      cookie,
    });
    function primaryFresh() {
      const age = Date.now() - Number(session.data.primaryVerifiedAt);
      if (
        !Number.isFinite(age) ||
        age < 0 ||
        age > 300000 ||
        !session.data.subject ||
        !session.data.tenant ||
        !session.data.loginChallenge ||
        !session.data.state
      )
        throw new Error("Primary authentication expired");
    }
    function secondFactorClaims() {
      return Number.isSafeInteger(session.data.mfaGeneration)
        ? {
            acr: passkeyAssurance,
            mfa_generation: session.data.mfaGeneration,
            amr: [session.data.primaryMethod, "mfa", "pop"],
          }
        : {};
    }
    async function acceptLogin() {
      primaryFresh();
      const assurance = secondFactorClaims();
      await config.passkeys.check({
        tenant_id: session.data.tenant,
        sub: session.data.subject,
        ...assurance,
      });
      const accepted = await adminRequest(
        config,
        "login",
        session.data.loginChallenge,
        {
          subject: session.data.subject,
          remember: false,
          ...(assurance.acr ? { acr: assurance.acr, amr: assurance.amr } : {}),
          context: {
            tenant_id: session.data.tenant,
            ...secondFactorClaims(),
          },
        },
      );
      return safeRedirect(accepted.redirect_to, config.origin);
    }
    async function acceptPrimary(
      subject: string,
      tenant: string,
      method: string,
    ) {
      await session.update({
        subject,
        tenant,
        primaryMethod: method,
        primaryVerifiedAt: Date.now(),
        mfaGeneration: null,
        localStartedAt: null,
        polisState: null,
        polisVerifier: null,
      });
      if (await config.passkeys.required(tenant, subject))
        return "/login?mfa=1";
      return acceptLogin();
    }
    if (path === "mfa/options" && event.method === "GET") {
      primaryFresh();
      const step = await config.passkeys.begin(
        session.data.tenant,
        session.data.subject,
        session.data.state,
      );
      if (!step) throw new Error("Second factor policy changed");
      return step;
    }
    if (path === "mfa/verify" && event.method === "POST") {
      primaryFresh();
      const proof = await readBody(event);
      const generation = await config.passkeys.verify(
        session.data.tenant,
        session.data.subject,
        session.data.state,
        proof,
      );
      await session.update({ mfaGeneration: generation });
      return sendRedirect(event, await acceptLogin(), 303);
    }
    if (path === "logout" && event.method === "POST") {
      deleteCookie(event, tokenCookie, cookie);
      deleteCookie(event, "__Host-galadril-profile", cookie);
      await session.clear();
      if (config.kratos && getCookie(event, "__Host-galadril-identity")) {
        const logout = await requestJson(
          new URL("/self-service/logout/browser", config.kratos),
          {
            headers: { cookie: identityCookie(getHeader(event, "cookie")) },
          },
        );
        const target = new URL(safeRedirect(logout.logout_url, config.origin));
        if (target.pathname !== "/identity/self-service/logout")
          throw new Error("Invalid identity logout redirect");
        return sendRedirect(event, target.href, 303);
      }
      return sendRedirect(event, "/login", 303);
    }
    if (path === "start" && event.method === "POST") {
      const body = await readBody<{ email: string }>(event);
      emailDomain(body?.email);
      await ensureClient(config);
      const state = random();
      const verifier = random();
      const nonce = random();
      await session.clear();
      await session.update({ state, verifier, nonce, email: body.email });
      const url = new URL("/oauth2/auth", config.origin);
      url.search = new URLSearchParams({
        client_id: "galadril-studio",
        response_type: "code",
        scope: "openid",
        audience: "galadril",
        redirect_uri: `${config.origin}/auth/callback`,
        state,
        nonce,
        code_challenge: challenge(verifier),
        code_challenge_method: "S256",
        prompt: "login",
      }).toString();
      if (getHeader(event, "accept")?.includes("application/json")) {
        const provider = await findLoginProvider(
          config.directory,
          body.email,
          Boolean(config.kratos),
        );
        return { redirect: url.href, local: provider.kind === "local" };
      }
      return sendRedirect(event, url.href, 303);
    }
    if (path === "login" && event.method === "GET") {
      if (!session.data.state) throw new Error("Missing flow");
      const loginChallenge = param("login_challenge");
      const login = await adminRequest(config, "login", loginChallenge);
      if (login.client?.client_id !== "galadril-studio")
        throw new Error("Invalid client");
      await session.update({ loginChallenge });
      const provider = await findLoginProvider(
        config.directory,
        session.data.email,
        Boolean(config.kratos),
      );
      if (provider.kind === "local") {
        await session.update({ localStartedAt: Date.now() });
        const url = new URL(
          "/identity/self-service/login/browser",
          config.origin,
        );
        url.search = new URLSearchParams({
          refresh: "true",
          return_to: `${config.origin}/auth/local/callback`,
        }).toString();
        return sendRedirect(event, url.href, 303);
      }
      const connection = provider.connection;
      const polisState = random();
      const polisVerifier = random();
      await session.update({
        polisState,
        polisVerifier,
        domain: connection.domain,
        connection: connection.connection_id,
      });
      const url = new URL("/api/oauth/authorize", config.origin);
      url.search = new URLSearchParams({
        client_id: connection.connection_id,
        response_type: "code",
        redirect_uri: `${config.origin}/auth/polis/callback`,
        state: polisState,
        code_challenge: challenge(polisVerifier),
        code_challenge_method: "S256",
      }).toString();
      return sendRedirect(event, url.href, 303);
    }
    if (path === "local/flow" && event.method === "GET") {
      if (
        !config.kratos ||
        !session.data.localStartedAt ||
        !session.data.loginChallenge
      )
        throw new Error("Missing local flow");
      const id = param("flow");
      if (!/^[0-9a-f-]{36}$/.test(id)) throw new Error("Invalid flow");
      const url = new URL("/self-service/login/flows", config.kratos);
      url.searchParams.set("id", id);
      const flow = await requestJson(url, {
        headers: { cookie: getHeader(event, "cookie") ?? "" },
      });
      const action = new URL(safeRedirect(flow.ui.action, config.origin));
      if (
        flow.type !== "browser" ||
        flow.return_to !== `${config.origin}/auth/local/callback` ||
        action.pathname !== "/identity/self-service/login" ||
        action.searchParams.get("flow") !== id
      )
        throw new Error("Invalid local flow binding");
      return {
        ...flow.ui,
        nodes: flow.ui.nodes.map((node: { attributes?: { name?: string } }) =>
          node.attributes?.name === "identifier"
            ? {
                ...node,
                attributes: {
                  ...node.attributes,
                  value: session.data.email,
                },
              }
            : node,
        ),
      };
    }
    if (path === "local/callback" && event.method === "GET") {
      if (!session.data.loginChallenge)
        throw new Error("Missing login challenge");
      const identity = await localIdentity(
        config,
        getHeader(event, "cookie"),
        session.data.localStartedAt,
      );
      return sendRedirect(
        event,
        await acceptPrimary(identity.subject, identity.tenant, "pwd"),
        303,
      );
    }
    if (path === "polis/callback" && event.method === "GET") {
      if (!constantEqual(session.data.polisState, param("state")))
        throw new Error("Invalid state");
      const connection = await findConnection(
        config.directory,
        `routing@${session.data.domain}`,
      );
      if (connection.connection_id !== session.data.connection)
        throw new Error("Connection changed");
      const result = await requestJson(
        new URL("/api/oauth/token", config.polis),
        {
          method: "POST",
          headers: { "content-type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            grant_type: "authorization_code",
            code: param("code"),
            client_id: connection.connection_id,
            client_secret: await connectionCredential(config, connection),
            redirect_uri: `${config.origin}/auth/polis/callback`,
            code_verifier: session.data.polisVerifier,
          }).toString(),
        },
      );
      const profile = await requestJson(
        new URL("/api/oauth/userinfo", config.polis),
        { headers: { authorization: `Bearer ${result.access_token}` } },
      );
      const subject = await resolveIdentity(
        config.directory,
        connection,
        externalSubject(profile, connection.connection_id),
        profile,
      );
      return sendRedirect(
        event,
        await acceptPrimary(subject, connection.tenant_id, "federated"),
        303,
      );
    }
    if (path === "consent" && event.method === "GET") {
      const consentChallenge = param("consent_challenge");
      const consent = await adminRequest(config, "consent", consentChallenge);
      const grants = validateConsent(consent);
      if (
        consent.subject !== session.data.subject ||
        consent.context?.tenant_id !== session.data.tenant
      )
        throw new Error("Invalid consent identity");
      await activeUser(config.directory, session.data.tenant, consent.subject);
      await config.passkeys.check({
        tenant_id: session.data.tenant,
        sub: consent.subject,
        ...secondFactorClaims(),
      });
      const accepted = await adminRequest(config, "consent", consentChallenge, {
        grant_scope: grants.scope,
        grant_access_token_audience: grants.audience,
        remember: false,
        session: {
          access_token: {
            tenant_id: session.data.tenant,
            token_use: "access",
            ...secondFactorClaims(),
          },
        },
      });
      return sendRedirect(
        event,
        safeRedirect(accepted.redirect_to, config.origin),
        303,
      );
    }
    if (path === "callback" && event.method === "GET") {
      if (!constantEqual(session.data.state, param("state")))
        throw new Error("Invalid state");
      const result = await requestJson(new URL("/oauth2/token", config.hydra), {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          "x-forwarded-proto": "https",
          authorization: `Basic ${Buffer.from(`galadril-studio:${config.clientSecret}`).toString("base64")}`,
        },
        body: new URLSearchParams({
          grant_type: "authorization_code",
          code: param("code"),
          redirect_uri: `${config.origin}/auth/callback`,
          code_verifier: session.data.verifier,
        }).toString(),
      });
      const identity = await jwtVerify(result.id_token, config.keys, {
        issuer: config.issuer,
        audience: "galadril-studio",
        algorithms: ["RS256"],
        requiredClaims: ["sub", "exp", "iat", "nonce"],
      });
      if (!constantEqual(session.data.nonce, identity.payload.nonce))
        throw new Error("Invalid nonce");
      const claims = await verifyAccessToken(
        result.access_token,
        config.issuer,
        config.keys,
      );
      if (
        claims.sub !== session.data.subject ||
        claims.tenant_id !== session.data.tenant ||
        identity.payload.sub !== claims.sub
      )
        throw new Error("Identity changed");
      await activeUser(config.directory, claims.tenant_id, claims.sub);
      await config.passkeys.check(claims);
      setCookie(event, tokenCookie, result.access_token, {
        ...cookie,
        maxAge: Math.max(0, claims.exp - Math.floor(Date.now() / 1000)),
      });
      const profile = await useSession(event, {
        name: "__Host-galadril-profile",
        password: config.sessionSecret,
        maxAge: Math.max(0, claims.exp - Math.floor(Date.now() / 1000)),
        cookie,
      });
      await profile.update({
        subject: claims.sub,
        tenant: claims.tenant_id,
        displayName: session.data.email,
      });
      await session.clear();
      return sendRedirect(event, "/", 303);
    }
    throw new Error("Invalid authentication route");
  } catch {
    outcome = "denied";
    throw createError({
      statusCode: 401,
      statusMessage: "Authentication unavailable or denied",
    });
  } finally {
    attempts.add(1, { operation, outcome });
  }
});
