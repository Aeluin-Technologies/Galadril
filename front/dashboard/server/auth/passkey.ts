import {
  generateAuthenticationOptions,
  generateRegistrationOptions,
  verifyAuthenticationResponse,
  verifyRegistrationResponse,
} from "@simplewebauthn/server";
import type {
  AuthenticationResponseJSON,
  RegistrationResponseJSON,
} from "@simplewebauthn/server";
import type { PasskeyStore } from "./contracts.ts";
import { record, text } from "#shared/records.ts";

/** Verifier inputs remain unknown until their wire-level credential envelope is checked. */
function credentialEnvelope(value: unknown): boolean {
  const credential = record(value);
  return (
    typeof credential.id === "string" &&
    typeof credential.rawId === "string" &&
    credential.type === "public-key" &&
    typeof credential.clientExtensionResults === "object" &&
    credential.clientExtensionResults !== null
  );
}

function registrationResponse(
  value: unknown,
): value is RegistrationResponseJSON {
  const response = record(record(value).response);
  return (
    credentialEnvelope(value) &&
    typeof response.clientDataJSON === "string" &&
    typeof response.attestationObject === "string" &&
    (response.transports === undefined ||
      (Array.isArray(response.transports) &&
        response.transports.every(
          (transport: unknown) =>
            typeof transport === "string" &&
            [
              "ble",
              "cable",
              "hybrid",
              "internal",
              "nfc",
              "smart-card",
              "usb",
            ].includes(transport),
        )))
  );
}

function authenticationResponse(
  value: unknown,
): value is AuthenticationResponseJSON {
  const response = record(record(value).response);
  return (
    credentialEnvelope(value) &&
    typeof response.clientDataJSON === "string" &&
    typeof response.authenticatorData === "string" &&
    typeof response.signature === "string" &&
    (response.userHandle === undefined ||
      typeof response.userHandle === "string")
  );
}

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
export function assertStepUpEligible(
  claimsValue: unknown,
  contextValue: unknown,
) {
  const claims = record(claimsValue);
  const context = record(contextValue);
  if (!context.enabled) throw new Error("Second factor policy changed");
  if (
    claims.acr === passkeyAssurance &&
    claims.mfa_generation !== context.generation
  )
    throw new ReauthenticationRequiredError();
}

/** A valid issuer signature alone does not satisfy a tenant's current MFA policy. */
export function assertMfaClaims(contextValue: unknown, claimsValue: unknown) {
  const context = record(contextValue);
  const claims = record(claimsValue);
  if (!context.enabled) return;
  if (
    claims.acr !== passkeyAssurance ||
    !Number.isSafeInteger(claims.mfa_generation) ||
    claims.mfa_generation !== context.generation ||
    !Array.isArray(context.credentials) ||
    !context.credentials.length
  )
    throw new SecondFactorRequiredError();
}

/** Delegates WebAuthn cryptography to the upstream verifier; storage owns atomicity. */
export class PasskeyService {
  readonly store: PasskeyStore;
  readonly origin: string;
  readonly rpID: string;
  constructor(store: PasskeyStore, origin: string) {
    this.store = store;
    this.origin = new URL(origin).origin;
    this.rpID = new URL(origin).hostname;
    if (!this.origin.startsWith("https://")) throw new Error("HTTPS required");
  }

  async begin(tenant: string, user: string, flow: string) {
    const context = await this.store.context(tenant, user);
    if (!context.enabled) return null;
    const step = !context.credentials.length
      ? {
          operation: "registration" as const,
          options: await generateRegistrationOptions({
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
          }),
        }
      : {
          operation: "authentication" as const,
          options: await generateAuthenticationOptions({
            rpID: this.rpID,
            userVerification: "required",
            allowCredentials: context.credentials.map(({ id, transports }) => ({
              id,
              transports,
            })),
          }),
        };
    await this.store.challenge(tenant, user, flow, {
      operation: step.operation,
      generation: context.generation,
      challenge: step.options.challenge,
    });
    return step;
  }

  async required(tenant: string, user: string) {
    return (await this.store.context(tenant, user)).enabled;
  }

  async check(claims: unknown) {
    const value = record(claims);
    assertMfaClaims(
      await this.store.context(text(value.tenant_id), text(value.sub)),
      claims,
    );
  }

  async verify(tenant: string, user: string, flow: string, response: unknown) {
    // Consumption commits before verification, so rejected proofs cannot be retried.
    const challenge = await this.store.consume(tenant, user, flow);
    if (!response || JSON.stringify(response).length > 65536)
      throw new Error("Invalid credential response");
    const context = await this.store.context(tenant, user);
    if (!context.enabled || context.generation !== challenge.generation)
      throw new Error("Second factor policy changed");
    const expected = {
      expectedChallenge: challenge.challenge,
      expectedOrigin: this.origin,
      expectedRPID: this.rpID,
      requireUserVerification: true,
    };
    if (challenge.operation === "registration") {
      if (!registrationResponse(response))
        throw new Error("Invalid credential response");
      if (context.credentials.length)
        throw new Error("Credential already enrolled");
      const result = await verifyRegistrationResponse({
        ...expected,
        response,
      });
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
      const envelope = record(response);
      const data = record(envelope.response);
      const assertion =
        data.userHandle === null
          ? { ...envelope, response: { ...data, userHandle: undefined } }
          : response;
      if (!authenticationResponse(assertion))
        throw new Error("Invalid credential response");
      const credential = context.credentials.find(
        (item) => item.id === assertion.id,
      );
      if (!credential) throw new Error("Unknown credential");
      const result = await verifyAuthenticationResponse({
        ...expected,
        response: assertion,
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
