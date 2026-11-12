import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  createSigningVault,
  unlockSigningKey,
} from "../app/utils/signing-vault.ts";
import {
  inspectSigningFields,
  parseSigningTrace,
  signPdf,
} from "../app/utils/pdf-signing.ts";
import {
  assertSignedPdf,
  compileSigningPdf,
  signingTrace,
} from "./signing-fixture.ts";

const scope = { tenant: "tenant-a", subject: "alice" };
const password = "a long unique signing password";

test("password and PRF unlock the same non-exportable signing key", async () => {
  const prf = crypto.getRandomValues(new Uint8Array(32));
  const vault = await createSigningVault(scope, password, {
    credential: "credential-fixture",
    prf,
    salt: crypto.getRandomValues(new Uint8Array(32)),
  });
  const stored: typeof vault = JSON.parse(JSON.stringify(vault));
  const byPassword = await unlockSigningKey(scope, stored, { password });
  const byPasskey = await unlockSigningKey(scope, stored, { prf });
  assert.equal(byPassword.extractable, false);
  assert.equal(byPasskey.extractable, false);
  const message = new TextEncoder().encode("signature fixture");
  assert.deepEqual(
    await crypto.subtle.sign("RSASSA-PKCS1-v1_5", byPassword, message),
    await crypto.subtle.sign("RSASSA-PKCS1-v1_5", byPasskey, message),
  );
  await assert.rejects(
    unlockSigningKey(scope, stored, { password: "incorrect password" }),
  );
  await assert.rejects(
    unlockSigningKey(scope, stored, { prf: new Uint8Array(32) }),
  );
  await assert.rejects(
    unlockSigningKey({ ...scope, subject: "bob" }, stored, { password }),
  );
  await assert.rejects(
    unlockSigningKey({ ...scope, tenant: "other" }, stored, { prf }),
  );
});

for (const visible of [true, false]) {
  test(
    `Tectonic PDF is signed with ${visible ? "a visible" : "an invisible"} field`,
    {
      skip: process.env.GALADRIL_SIGNING_E2E !== "1",
    },
    async () => {
      const dir = mkdtempSync(join(tmpdir(), "galadril-pdf-signing-"));
      try {
        const original = compileSigningPdf(dir, visible);
        assert.deepEqual(
          await inspectSigningFields(original),
          visible ? ["galadril:claimant"] : [],
        );
        const vault = await createSigningVault(scope, password);
        const key = await unlockSigningKey(scope, vault, { password });
        const signed = await signPdf(
          original,
          vault,
          key,
          visible
            ? {
                field: "galadril:claimant",
                trace: parseSigningTrace(JSON.stringify(signingTrace)),
              }
            : null,
        );
        const path = join(dir, "signed.pdf");
        writeFileSync(path, signed);
        await assertSignedPdf(path, original, visible);
        const placement = {
          page: 0,
          rect: [30, 30, 190, 85],
        } as const;
        const placed = await signPdf(original, vault, key, {
          field: visible ? "galadril:claimant" : "",
          trace: parseSigningTrace(JSON.stringify(signingTrace)),
          placement,
          caption: ["Electronically signed by Alice", "On 12 November"],
        });
        const placedPath = join(dir, "placed.pdf");
        writeFileSync(placedPath, placed);
        await assertSignedPdf(placedPath, original, true, placement);
        if (visible) {
          const pdfsig = process.env.GALADRIL_PDFSIG ?? "pdfsig";
          const changed = Buffer.from(signed);
          assert.match(changed.toString("ascii", 0, 8), /^%PDF-1\.[0-7]$/);
          changed.writeUInt8(changed.at(7) === 55 ? 54 : 55, 7);
          const tampered = join(dir, "tampered.pdf");
          writeFileSync(tampered, changed);
          const invalid = spawnSync(pdfsig, ["-nocert", tampered], {
            encoding: "utf8",
            timeout: 30_000,
          });
          assert.match(invalid.stdout, /Signature Validation: Digest Mismatch/);

          const other = { ...scope, subject: "bob" };
          const secondVault = await createSigningVault(other, password);
          assert.notEqual(secondVault.certificate, vault.certificate);
          const secondKey = await unlockSigningKey(other, secondVault, {
            password,
          });
          const countersigned = await signPdf(
            signed,
            secondVault,
            secondKey,
            null,
          );
          assert.deepEqual(countersigned.subarray(0, signed.length), signed);
          const countersignedPath = join(dir, "countersigned.pdf");
          writeFileSync(countersignedPath, countersigned);
          const signatures = execFileSync(
            pdfsig,
            ["-nocert", countersignedPath],
            {
              encoding: "utf8",
              timeout: 30_000,
            },
          );
          assert.equal(
            signatures.match(/Signature Validation: Signature is Valid/g)
              ?.length,
            2,
          );
        }
      } finally {
        rmSync(dir, { recursive: true, force: true });
      }
    },
  );
}
