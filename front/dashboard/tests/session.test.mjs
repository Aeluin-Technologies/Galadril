import assert from "node:assert/strict";
import test from "node:test";
import { refreshSession, shouldRenew } from "../server/auth/renewal.mjs";
import * as renewal from "../server/auth/renewal.mjs";
import { beginStepUp } from "../app/utils/auth-navigation.mjs";
import {
  assertStepUpEligible,
  passkeyAssurance,
} from "../server/auth/passkey.mjs";

test("in-place enrollment never reuses a passkey session invalidated by an administrator", () => {
  assert.doesNotThrow(() =>
    assertStepUpEligible({}, { enabled: true, generation: 0 }),
  );
  assert.doesNotThrow(() =>
    assertStepUpEligible(
      { acr: passkeyAssurance, mfa_generation: 2 },
      { enabled: true, generation: 2 },
    ),
  );
  assert.throws(
    () =>
      assertStepUpEligible(
        { acr: passkeyAssurance, mfa_generation: 1 },
        { enabled: true, generation: 2 },
      ),
    /sign in again/i,
  );
  assert.throws(() => assertStepUpEligible({}, { enabled: false }), /policy/i);
});

test("passkey step-up keeps credentials same-origin and only accepts the MFA gate", async () => {
  let calls = 0;
  await beginStepUp("https://app.example", async (url, options) => {
    calls++;
    assert.equal(options.credentials, "same-origin");
    return calls === 1
      ? {
          ok: true,
          json: async () => ({
            redirect: "https://app.example/oauth2/auth?state=bound",
          }),
        }
      : { ok: true, url: "https://app.example/login?mfa=1" };
  });
  assert.equal(calls, 2);
  await assert.rejects(
    beginStepUp("https://app.example", async () => ({
      ok: true,
      json: async () => ({ redirect: "https://evil.example/oauth2/auth" }),
    })),
  );
});

test("session renewal is proactive and bounded by idle and absolute expiry", () => {
  assert.equal(shouldRenew(1200, 1000), false);
  assert.equal(shouldRenew(1100, 1000), true);
  assert.equal(shouldRenew(undefined, 1000), true);
});

test("local logout clears credentials even when remote revocation fails", async () => {
  const calls = [];
  await assert.rejects(
    renewal.logoutSession("private-refresh-token", {
      async revoke(token) {
        assert.equal(token, "private-refresh-token");
        calls.push("revoke");
        throw new Error("Provider unavailable");
      },
      async clear() {
        calls.push("clear");
      },
      async prepareIdentityLogout() {
        assert.fail("Failed revocation must still clear local credentials");
      },
    }),
    /Provider unavailable/,
  );
  assert.deepEqual(calls, ["revoke", "clear"]);
  calls.length = 0;
  await renewal.logoutSession(undefined, {
    async revoke() {
      assert.fail("There is no remote credential to revoke");
    },
    async clear() {
      calls.push("clear");
    },
  });
  assert.deepEqual(calls, ["clear"]);
});

test("native identity logout is prepared before cookie cleanup, including sessions without refresh secrets", async () => {
  for (const token of ["private-refresh-token", undefined]) {
    const calls = [];
    await renewal.logoutSession(token, {
      async revoke() {
        calls.push("revoke");
      },
      async prepareIdentityLogout() {
        calls.push("native-logout");
      },
      async clear() {
        calls.push("clear");
      },
    });
    assert.deepEqual(
      calls,
      token ? ["revoke", "native-logout", "clear"] : ["native-logout", "clear"],
    );
  }
});

test("failed native logout preparation still clears all local credentials", async () => {
  const calls = [];
  await assert.rejects(
    renewal.logoutSession("private-refresh-token", {
      async revoke() {
        calls.push("revoke");
      },
      async prepareIdentityLogout() {
        calls.push("native-logout");
        throw new Error("Identity provider unavailable");
      },
      async clear() {
        calls.push("clear");
      },
    }),
    /Identity provider unavailable/,
  );
  assert.deepEqual(calls, ["revoke", "native-logout", "clear"]);
});

test("sign-in and renewal reject missing refresh secrets before persisting credentials", () => {
  for (const refresh_token of [undefined, null, "", 12, {}, []])
    assert.throws(
      () => renewal.requireRefreshToken({ refresh_token }),
      /refresh token/,
    );
  assert.equal(
    renewal.requireRefreshToken({ refresh_token: "private" }),
    "private",
  );
});

test("refresh verifies identity and live permissions before returning rotated secrets", async () => {
  const state = {
    subject: "alice",
    tenant: "demo",
    refreshToken: "private",
    createdAt: 1000,
    lastActiveAt: 2000,
  };
  const calls = [];
  const config = {
    async exchange(token) {
      assert.equal(token, "private");
      return { access_token: "access", refresh_token: "rotated" };
    },
    async verify(token) {
      assert.equal(token, "access");
      return { sub: "alice", tenant_id: "demo", exp: 8000 };
    },
    async active(tenant, subject) {
      calls.push([tenant, subject]);
    },
    async check(claims) {
      assert.equal(claims.sub, "alice");
    },
  };
  const result = await refreshSession(state, config, 3000);
  assert.equal(result.refreshToken, "rotated");
  assert.equal(result.createdAt, 1000);
  assert.deepEqual(calls, [["demo", "alice"]]);
  await assert.rejects(
    refreshSession(
      state,
      { ...config, verify: async () => ({ sub: "bob", tenant_id: "demo" }) },
      3000,
    ),
    /identity/i,
  );
  await assert.rejects(
    refreshSession(state, config, 2000 + 30 * 60 * 1000 + 1),
    /expired/i,
  );
  await assert.rejects(
    refreshSession({ ...state, createdAt: -8 * 60 * 60 * 1000 }, config, 3000),
    /expired/i,
  );
  await assert.rejects(
    refreshSession(
      state,
      {
        ...config,
        check: async () => {
          throw new Error("Second factor required");
        },
      },
      3000,
    ),
    /Second factor/,
  );
});
