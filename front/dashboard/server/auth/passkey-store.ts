/** GraphQL transports public WebAuthn material; Gateway owns atomic persistence. */
import type {
  AuthDirectory,
  PasskeyChallenge,
  PasskeyContext,
  StoredCredential,
} from "./contracts.ts";
import type { AuthenticatorTransport } from "@simplewebauthn/server";
import { parseObject, records, text } from "#shared/records.ts";

function transport(value: unknown): value is AuthenticatorTransport {
  return (
    typeof value === "string" &&
    ["ble", "cable", "hybrid", "internal", "nfc", "smart-card", "usb"].includes(
      value,
    )
  );
}

export class GatewayPasskeyStore {
  readonly directory: AuthDirectory;
  constructor(directory: AuthDirectory) {
    this.directory = directory;
  }

  async context(tenant: string, user: string): Promise<PasskeyContext> {
    const context = parseObject(
      await this.directory.request("passkeyContext", { tenant, user }),
    );
    if (!context.enabled)
      return {
        enabled: false,
        generation: 0,
        userHandle: new Uint8Array(),
        credentials: [],
      };
    if (
      typeof context.generation !== "number" ||
      !Number.isSafeInteger(context.generation)
    )
      throw new Error("Invalid passkey generation");
    return {
      enabled: true,
      generation: context.generation,
      userHandle: Buffer.from(text(context.userHandle), "base64url"),
      credentials: records(context.credentials).map(
        (credential): StoredCredential => {
          if (
            typeof credential.counter !== "number" ||
            !Number.isSafeInteger(credential.counter)
          )
            throw new Error("Invalid passkey counter");
          return {
            id: text(credential.id),
            counter: credential.counter,
            deviceType:
              credential.deviceType === "multiDevice"
                ? "multiDevice"
                : "singleDevice",
            backedUp: credential.backedUp === true,
            transports: Array.isArray(credential.transports)
              ? credential.transports.filter(transport)
              : [],
            publicKey: new Uint8Array(
              Buffer.from(text(credential.publicKey), "base64url"),
            ),
          };
        },
      ),
    };
  }

  challenge(
    tenant: string,
    user: string,
    flow: string,
    value: PasskeyChallenge,
  ) {
    return this.directory.request("challenge", {
      tenant,
      user,
      flow,
      value: JSON.stringify(value),
    });
  }

  async consume(
    tenant: string,
    user: string,
    flow: string,
  ): Promise<PasskeyChallenge> {
    const value = parseObject(
      await this.directory.request("consume", { tenant, user, flow }),
    );
    if (
      typeof value.generation !== "number" ||
      !Number.isSafeInteger(value.generation)
    )
      throw new Error("Invalid passkey generation");
    return {
      operation: text(value.operation),
      generation: value.generation,
      challenge: text(value.challenge),
    };
  }

  save(
    tenant: string,
    user: string,
    generation: number,
    credential: StoredCredential,
    previousCounter?: number,
  ) {
    return this.directory.request("saveCredential", {
      tenant,
      user,
      generation: String(generation),
      previousCounter:
        previousCounter === undefined ? null : String(previousCounter),
      credential: JSON.stringify({
        id: credential.id,
        publicKey: Buffer.from(credential.publicKey).toString("base64url"),
        counter: credential.counter,
        transports: credential.transports ?? [],
        deviceType: credential.deviceType,
        backedUp: credential.backedUp,
      }),
    });
  }
}
