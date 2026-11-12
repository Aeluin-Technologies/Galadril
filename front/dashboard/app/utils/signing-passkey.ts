import { decodeSigningBytes, encodeSigningBytes } from "./signing-vault.ts";
import type { PasskeyWrap } from "./signing-vault.ts";

function random() {
  return crypto.getRandomValues(new Uint8Array(32));
}

export async function signingPasskeySecret(
  credential: string,
  salt: Uint8Array<ArrayBuffer>,
) {
  const result = await navigator.credentials.get({
    publicKey: {
      challenge: random(),
      allowCredentials: [
        { type: "public-key", id: decodeSigningBytes(credential) },
      ],
      userVerification: "required",
      extensions: { prf: { eval: { first: salt } } },
    },
  });
  if (
    !(result instanceof PublicKeyCredential) ||
    result.id !== credential ||
    !(result.response instanceof AuthenticatorAssertionResponse) ||
    !((new Uint8Array(result.response.authenticatorData).at(32) ?? 0) & 4)
  )
    throw new Error("Passkey verification failed");
  const output = result.getClientExtensionResults().prf?.results?.first;
  if (!output || output.byteLength !== 32)
    throw new Error("Passkey PRF is unavailable");
  return new Uint8Array(
    output instanceof ArrayBuffer
      ? output
      : output.buffer.slice(
          output.byteOffset,
          output.byteOffset + output.byteLength,
        ),
  );
}

export async function enrollSigningPasskey() {
  const salt = random();
  const created = await navigator.credentials.create({
    publicKey: {
      challenge: random(),
      rp: { name: "Galadril" },
      user: {
        id: random(),
        name: "galadril-pdf-signing",
        displayName: "Galadril PDF signing",
      },
      pubKeyCredParams: [
        { type: "public-key", alg: -7 },
        { type: "public-key", alg: -257 },
      ],
      authenticatorSelection: {
        userVerification: "required",
        residentKey: "preferred",
      },
      attestation: "none",
      extensions: { prf: { eval: { first: salt } } },
    },
  });
  if (
    !(created instanceof PublicKeyCredential) ||
    !created.getClientExtensionResults().prf?.enabled
  )
    throw new Error("Passkey PRF is unavailable");
  const credential = encodeSigningBytes(new Uint8Array(created.rawId));
  return {
    credential,
    salt,
    prf: await signingPasskeySecret(credential, salt),
  };
}

export function unlockSigningPasskey(slot: PasskeyWrap) {
  return signingPasskeySecret(slot.credential, decodeSigningBytes(slot.salt));
}
