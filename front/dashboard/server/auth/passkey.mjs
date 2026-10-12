import {
  generateAuthenticationOptions,
  generateRegistrationOptions,
  verifyAuthenticationResponse,
  verifyRegistrationResponse,
} from "@simplewebauthn/server";

export const passkeyAssurance = "urn:galadril:webauthn:uv";

export class SecondFactorRequiredError extends Error {
  constructor() {
    super("Second factor required");
  }
}
export class ReauthenticationRequiredError extends Error {
  constructor() {
    super("Please sign in again after passkey recovery");
  }
}

/** Recovery invalidates prior MFA sessions; they must repeat primary authentication. */
export function assertStepUpEligible(claims, context) {
  if (!context.enabled) throw new Error("Second factor policy changed");
  if (
    claims.acr === passkeyAssurance &&
    claims.mfa_generation !== context.generation
  )
    throw new ReauthenticationRequiredError();
}

/** A valid issuer signature alone does not satisfy a tenant's current MFA policy. */
export function assertMfaClaims(context, claims) {
  if (!context.enabled) return;
  if (
    claims.acr !== passkeyAssurance ||
    !Number.isSafeInteger(claims.mfa_generation) ||
    claims.mfa_generation !== context.generation ||
    !context.credentials.length
  )
    throw new SecondFactorRequiredError();
}

/** Delegates WebAuthn cryptography to the upstream verifier; storage owns atomicity. */
export class PasskeyService {
  constructor(store, origin) {
    this.store = store;
    this.origin = new URL(origin).origin;
    this.rpID = new URL(origin).hostname;
    if (!this.origin.startsWith("https://")) throw new Error("HTTPS required");
  }

  async begin(tenant, user, flow) {
    const context = await this.store.context(tenant, user);
    if (!context.enabled) return null;
    const operation = context.credentials.length
      ? "authentication"
      : "registration";
    const options =
      operation === "registration"
        ? await generateRegistrationOptions({
            rpName: "Galadril",
            rpID: this.rpID,
            userID: new Uint8Array(context.userHandle),
            userName: user,
            attestationType: "none",
            supportedAlgorithmIDs: [-7, -257],
            authenticatorSelection: {
              residentKey: "preferred",
              userVerification: "required",
            },
          })
        : await generateAuthenticationOptions({
            rpID: this.rpID,
            userVerification: "required",
            allowCredentials: context.credentials.map(({ id, transports }) => ({
              id,
              transports,
            })),
          });
    await this.store.challenge(tenant, user, flow, {
      operation,
      generation: context.generation,
      challenge: options.challenge,
    });
    return { operation, options };
  }

  async required(tenant, user) {
    return (await this.store.context(tenant, user)).enabled;
  }

  async check(claims) {
    assertMfaClaims(
      await this.store.context(claims.tenant_id, claims.sub),
      claims,
    );
  }

  async verify(tenant, user, flow, response) {
    // Consumption commits before verification, so rejected proofs cannot be retried.
    const challenge = await this.store.consume(tenant, user, flow);
    if (!response || JSON.stringify(response).length > 65536)
      throw new Error("Invalid credential response");
    const context = await this.store.context(tenant, user);
    if (!context.enabled || context.generation !== challenge.generation)
      throw new Error("Second factor policy changed");
    const expected = {
      response,
      expectedChallenge: challenge.challenge,
      expectedOrigin: this.origin,
      expectedRPID: this.rpID,
      requireUserVerification: true,
    };
    if (challenge.operation === "registration") {
      if (context.credentials.length)
        throw new Error("Credential already enrolled");
      const result = await verifyRegistrationResponse(expected);
      if (!result.verified || !result.registrationInfo)
        throw new Error("Invalid registration");
      const { credential, credentialDeviceType, credentialBackedUp } =
        result.registrationInfo;
      await this.store.save(tenant, user, challenge.generation, {
        ...credential,
        deviceType: credentialDeviceType,
        backedUp: credentialBackedUp,
      });
    } else if (challenge.operation === "authentication") {
      const credential = context.credentials.find(
        (item) => item.id === response?.id,
      );
      if (!credential) throw new Error("Unknown credential");
      const result = await verifyAuthenticationResponse({
        ...expected,
        credential,
      });
      if (!result.verified) throw new Error("Invalid assertion");
      await this.store.save(
        tenant,
        user,
        challenge.generation,
        {
          ...credential,
          counter: result.authenticationInfo.newCounter,
          backedUp: result.authenticationInfo.credentialBackedUp,
        },
        credential.counter,
      );
    } else throw new Error("Invalid challenge operation");
    return challenge.generation;
  }
}
