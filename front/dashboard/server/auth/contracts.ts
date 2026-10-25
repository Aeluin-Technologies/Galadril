import type { JWTPayload } from "jose";
import type {
  WebAuthnCredential,
  CredentialDeviceType,
} from "@simplewebauthn/server";

/** Only the private authentication schema can resolve canonical identities. */
export interface AuthDirectory {
  request(
    operation: string,
    variables: Record<string, unknown>,
  ): Promise<unknown>;
}

export interface AuthConnection {
  domain: string;
  connection_id: string;
  tenant_id: string;
}

export interface AccessClaims extends JWTPayload {
  sub: string;
  tenant_id: string;
  iat: number;
  exp: number;
  token_use: "access";
}

export interface StoredCredential extends WebAuthnCredential {
  deviceType: CredentialDeviceType;
  backedUp: boolean;
}

export interface PasskeyContext {
  enabled: boolean;
  generation: number;
  userHandle: Uint8Array;
  credentials: StoredCredential[];
}

export interface PasskeyChallenge {
  operation: string;
  generation: number;
  challenge: string;
}

/** Persistence implementations must consume challenges before accepting cryptographic proofs. */
export interface PasskeyStore {
  context(tenant: string, user: string): Promise<PasskeyContext>;
  challenge(
    tenant: string,
    user: string,
    flow: string,
    value: PasskeyChallenge,
  ): Promise<unknown>;
  consume(
    tenant: string,
    user: string,
    flow: string,
  ): Promise<PasskeyChallenge>;
  save(
    tenant: string,
    user: string,
    generation: number,
    credential: StoredCredential,
    previousCounter?: number,
  ): Promise<unknown>;
}
