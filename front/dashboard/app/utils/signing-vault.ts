import "reflect-metadata";
import {
  KeyUsageFlags,
  KeyUsagesExtension,
  X509CertificateGenerator,
} from "@peculiar/x509";

export interface SigningScope {
  tenant: string;
  subject: string;
}

interface KeyWrap {
  salt: string;
  nonce: string;
  ciphertext: string;
}

export interface PasskeyWrap extends KeyWrap {
  credential: string;
}

export interface SigningVault {
  version: 1;
  certificate: string;
  password: KeyWrap;
  passkey?: PasskeyWrap;
}

export type UnlockFactor =
  { password: string } | { prf: Uint8Array<ArrayBuffer> };

export function encodeSigningBytes(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
}

export function decodeSigningBytes(encoded: string): Uint8Array<ArrayBuffer> {
  if (!/^[A-Za-z0-9_-]+$/.test(encoded) || encoded.length > 8192)
    throw new Error("Invalid signing vault");
  return Uint8Array.from(
    atob(encoded.replaceAll("-", "+").replaceAll("_", "/")),
    (character) => character.charCodeAt(0),
  );
}

const rsa = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" } as const;
const encoder = new TextEncoder();
const aes = { name: "AES-GCM", length: 256 };
const minimumPasswordLength = 15;

async function derive(
  factor: UnlockFactor,
  salt: Uint8Array<ArrayBuffer>,
  usage: KeyUsage,
) {
  if ("password" in factor) {
    const secret = encoder.encode(factor.password);
    if (!secret.length || secret.length > 1024)
      throw new Error("Invalid signing password");
    try {
      const material = await crypto.subtle.importKey(
        "raw",
        secret,
        "PBKDF2",
        false,
        ["deriveKey"],
      );
      return await crypto.subtle.deriveKey(
        { name: "PBKDF2", salt, hash: "SHA-256", iterations: 600_000 },
        material,
        aes,
        false,
        [usage],
      );
    } finally {
      secret.fill(0);
    }
  }
  if (factor.prf.byteLength !== 32) throw new Error("Invalid passkey secret");
  const material = await crypto.subtle.importKey(
    "raw",
    factor.prf,
    "HKDF",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    {
      name: "HKDF",
      salt,
      hash: "SHA-256",
      info: encoder.encode("Galadril signing vault v1"),
    },
    material,
    aes,
    false,
    [usage],
  );
}

function associatedData(
  scope: SigningScope,
  certificate: string,
  method: string,
  credential?: string,
) {
  return encoder.encode(
    JSON.stringify([
      "Galadril signing vault",
      1,
      scope.tenant,
      scope.subject,
      certificate,
      method,
      credential ?? null,
    ]),
  );
}

interface PasskeyEnrollment {
  credential: string;
  prf: Uint8Array<ArrayBuffer>;
  salt: Uint8Array<ArrayBuffer>;
}

export async function createSigningVault(
  scope: SigningScope,
  password: string,
  passkey?: PasskeyEnrollment,
): Promise<SigningVault> {
  if ([...password].length < minimumPasswordLength)
    throw new Error("Signing password is too short");
  const keys = await crypto.subtle.generateKey(
    { ...rsa, modulusLength: 3072, publicExponent: new Uint8Array([1, 0, 1]) },
    true,
    ["sign", "verify"],
  );
  const notBefore = new Date(Date.now() - 60_000);
  const notAfter = new Date(notBefore);
  notAfter.setUTCFullYear(notAfter.getUTCFullYear() + 5);
  const serial = crypto.randomUUID().replaceAll("-", "");
  const certificate = await X509CertificateGenerator.createSelfSigned(
    {
      serialNumber: serial,
      name: `CN=Galadril ${serial}`,
      notBefore,
      notAfter,
      signingAlgorithm: rsa,
      keys,
      extensions: [
        new KeyUsagesExtension(
          KeyUsageFlags.digitalSignature | KeyUsageFlags.nonRepudiation,
          true,
        ),
      ],
    },
    crypto,
  );
  const encoded = encodeSigningBytes(new Uint8Array(certificate.rawData));

  async function wrap(
    factor: UnlockFactor,
    salt: Uint8Array<ArrayBuffer>,
    credential?: string,
  ): Promise<KeyWrap> {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const key = await derive(factor, salt, "wrapKey");
    const additionalData = associatedData(
      scope,
      encoded,
      credential ? "passkey" : "password",
      credential,
    );
    const ciphertext = await crypto.subtle.wrapKey(
      "pkcs8",
      keys.privateKey,
      key,
      { name: "AES-GCM", iv, additionalData },
    );
    return {
      salt: encodeSigningBytes(salt),
      nonce: encodeSigningBytes(iv),
      ciphertext: encodeSigningBytes(new Uint8Array(ciphertext)),
    };
  }

  return {
    version: 1,
    certificate: encoded,
    password: await wrap(
      { password },
      crypto.getRandomValues(new Uint8Array(32)),
    ),
    ...(passkey
      ? {
          passkey: {
            ...(await wrap(
              { prf: passkey.prf },
              passkey.salt,
              passkey.credential,
            )),
            credential: passkey.credential,
          },
        }
      : {}),
  };
}

export async function unlockSigningKey(
  scope: SigningScope,
  vault: SigningVault,
  factor: UnlockFactor,
): Promise<CryptoKey> {
  if (vault.version !== 1) throw new Error("Unsupported signing vault");
  const method = "password" in factor ? "password" : "passkey";
  const slot = vault[method];
  if (!slot) throw new Error("Unlock method unavailable");
  const credential =
    method === "passkey" ? vault.passkey?.credential : undefined;
  const salt = decodeSigningBytes(slot.salt);
  const iv = decodeSigningBytes(slot.nonce);
  if (salt.length !== 32 || iv.length !== 12)
    throw new Error("Invalid signing vault");
  const key = await derive(factor, salt, "unwrapKey");
  return crypto.subtle.unwrapKey(
    "pkcs8",
    decodeSigningBytes(slot.ciphertext),
    key,
    {
      name: "AES-GCM",
      iv,
      additionalData: associatedData(
        scope,
        vault.certificate,
        method,
        credential,
      ),
    },
    rsa,
    false,
    ["sign"],
  );
}
