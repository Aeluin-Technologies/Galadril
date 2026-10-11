import assert from "node:assert/strict";
import test from "node:test";
import {
  createHash,
  generateKeyPairSync,
  randomBytes,
  sign,
} from "node:crypto";
import { isoCBOR } from "@simplewebauthn/server/helpers";
import { PasskeyService, assertMfaClaims } from "../server/auth/passkey.mjs";

function fixture(enabled = true) {
  const credentials = [];
  const challenges = new Map();
  const context = {
    enabled,
    generation: 0,
    userHandle: randomBytes(32),
    credentials,
  };
  const store = {
    context: async () => context,
    challenge: async (tenant, user, flow, value) =>
      challenges.set(`${tenant}:${user}:${flow}`, value),
    consume: async (tenant, user, flow) => {
      const key = `${tenant}:${user}:${flow}`;
      const value = challenges.get(key);
      challenges.delete(key);
      if (!value) throw new Error("Missing challenge");
      return value;
    },
    save: async (_tenant, _user, generation, credential, previousCounter) => {
      assert.equal(generation, context.generation);
      if (previousCounter === undefined) {
        assert.equal(credentials.length, 0);
        credentials.push(credential);
      } else {
        const existing = credentials.find((item) => item.id === credential.id);
        assert.equal(existing.counter, previousCounter);
        Object.assign(existing, credential);
      }
    },
  };
  return { context, service: new PasskeyService(store, "https://app.example") };
}

function authenticator() {
  const { privateKey, publicKey } = generateKeyPairSync("ec", {
    namedCurve: "prime256v1",
  });
  const jwk = publicKey.export({ format: "jwk" });
  const id = randomBytes(32);
  const publicKeyCose = isoCBOR.encode(
    new Map([
      [1, 2],
      [3, -7],
      [-1, 1],
      [-2, Buffer.from(jwk.x, "base64url")],
      [-3, Buffer.from(jwk.y, "base64url")],
    ]),
  );
  const rpHash = createHash("sha256").update("app.example").digest();
  function response(
    options,
    registration,
    { origin = "https://app.example", uv = true, counter = 1 } = {},
  ) {
    const clientDataJSON = Buffer.from(
      JSON.stringify({
        type: registration ? "webauthn.create" : "webauthn.get",
        challenge: options.challenge,
        origin,
        crossOrigin: false,
      }),
    );
    const count = Buffer.alloc(4);
    count.writeUInt32BE(registration ? 0 : counter);
    const flags = Buffer.from([1 | (uv ? 4 : 0) | (registration ? 64 : 0)]);
    const length = Buffer.alloc(2);
    length.writeUInt16BE(id.length);
    const authData = Buffer.concat([
      rpHash,
      flags,
      count,
      ...(registration
        ? [Buffer.alloc(16), length, id, Buffer.from(publicKeyCose)]
        : []),
    ]);
    return {
      id: id.toString("base64url"),
      rawId: id.toString("base64url"),
      type: "public-key",
      clientExtensionResults: {},
      response: registration
        ? {
            clientDataJSON: clientDataJSON.toString("base64url"),
            attestationObject: Buffer.from(
              isoCBOR.encode(
                new Map([
                  ["fmt", "none"],
                  ["attStmt", new Map()],
                  ["authData", authData],
                ]),
              ),
            ).toString("base64url"),
            transports: ["internal"],
          }
        : {
            clientDataJSON: clientDataJSON.toString("base64url"),
            authenticatorData: authData.toString("base64url"),
            signature: sign(
              "sha256",
              Buffer.concat([
                authData,
                createHash("sha256").update(clientDataJSON).digest(),
              ]),
              privateKey,
            ).toString("base64url"),
            userHandle: null,
          },
    };
  }
  return { response };
}

test("passkey policy defaults off and validates proof generations", async () => {
  const { service } = fixture(false);
  assert.equal(await service.begin("tenant", "user", "flow"), null);
  assertMfaClaims({ enabled: false }, {});
  assert.throws(() =>
    assertMfaClaims({ enabled: true, generation: 1, credentials: [{}] }, {}),
  );
  assertMfaClaims(
    { enabled: true, generation: 1, credentials: [{}] },
    { acr: "urn:galadril:webauthn:uv", mfa_generation: 1 },
  );
  assert.throws(() =>
    assertMfaClaims(
      { enabled: true, generation: 2, credentials: [{}] },
      { acr: "urn:galadril:webauthn:uv", mfa_generation: 1 },
    ),
  );
});

test("first sign-in enrolls a verified passkey and subsequent logins require an assertion", async () => {
  const { service, context } = fixture();
  const key = authenticator();
  let step = await service.begin("tenant", "user", "flow");
  assert.equal(step.operation, "registration");
  assert.equal(
    step.options.authenticatorSelection.userVerification,
    "required",
  );
  assert.equal(
    await service.verify(
      "tenant",
      "user",
      "flow",
      key.response(step.options, true),
    ),
    0,
  );
  assert.equal(context.credentials.length, 1);
  await assert.rejects(
    service.verify("tenant", "user", "flow", key.response(step.options, true)),
  );
  step = await service.begin("tenant", "user", "flow2");
  assert.equal(step.operation, "authentication");
  assert.equal(step.options.userVerification, "required");
  assert.equal(
    await service.verify(
      "tenant",
      "user",
      "flow2",
      key.response(step.options, false),
    ),
    0,
  );
  assert.equal(context.credentials[0].counter, 1);
});

test("passkey verification rejects foreign origins, missing UV, replay, tenant substitution and reset races", async () => {
  for (const patch of [{ origin: "https://foreign.example" }, { uv: false }]) {
    const { service } = fixture();
    const key = authenticator();
    const step = await service.begin("tenant", "user", "flow");
    await assert.rejects(
      service.verify(
        "tenant",
        "user",
        "flow",
        key.response(step.options, true, patch),
      ),
    );
    await assert.rejects(
      service.verify(
        "tenant",
        "user",
        "flow",
        key.response(step.options, true),
      ),
    );
  }
  const { service, context } = fixture();
  const key = authenticator();
  const step = await service.begin("tenant", "user", "flow");
  await assert.rejects(
    service.verify("other", "user", "flow", key.response(step.options, true)),
  );
  context.generation += 1;
  await assert.rejects(
    service.verify("tenant", "user", "flow", key.response(step.options, true)),
  );
});
