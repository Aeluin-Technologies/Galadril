/** GraphQL transports public WebAuthn material; Gateway owns atomic persistence. */
export class GatewayPasskeyStore {
  constructor(directory) {
    this.directory = directory;
  }

  async context(tenant, user) {
    const context = JSON.parse(
      await this.directory.request("passkeyContext", { tenant, user }),
    );
    if (!context.enabled) return context;
    return {
      ...context,
      userHandle: Buffer.from(context.userHandle, "base64url"),
      credentials: context.credentials.map((credential) => ({
        ...credential,
        publicKey: new Uint8Array(
          Buffer.from(credential.publicKey, "base64url"),
        ),
      })),
    };
  }

  challenge(tenant, user, flow, value) {
    return this.directory.request("challenge", {
      tenant,
      user,
      flow,
      value: JSON.stringify(value),
    });
  }

  async consume(tenant, user, flow) {
    return JSON.parse(
      await this.directory.request("consume", { tenant, user, flow }),
    );
  }

  save(tenant, user, generation, credential, previousCounter) {
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
